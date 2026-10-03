// Protocol-only forwarding probe. No media encoder, decoder or display runs
// inside the timed interval. Input is a pre-encoded, non-private test fixture.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/bluenviron/gortmplib"
	"github.com/bluenviron/gortmplib/pkg/codecs"
	"github.com/bluenviron/gortsplib/v5"
	"github.com/bluenviron/gortsplib/v5/pkg/base"
	"github.com/bluenviron/gortsplib/v5/pkg/format"
	"github.com/bluenviron/gortsplib/v5/pkg/format/rtph264"
	"github.com/bluenviron/mediacommon/v2/pkg/codecs/mpeg4audio"
	"github.com/pion/rtp"
)

type packet struct {
	Video bool
	DTSMS int64
	PTSMS int64
	NALUs [][]byte
	Data  []byte
}
type fixture struct {
	SPS         []byte
	PPS         []byte
	AudioConfig []byte
	LoopMS      int64
	Packets     []packet
}
type config struct {
	PublishURL string
	ReadURL    string
	Fixture    string
	Seconds    int
	Readers    int
	SlowReader bool
}
type sent struct {
	at     time.Time
	digest [32]byte
	tick   uint32
	seq    int
}
type sample struct {
	AtSeconds    float64 `json:"at_seconds"`
	Milliseconds float64 `json:"milliseconds"`
	Sequence     int     `json:"sequence"`
}
type result struct {
	Samples    []sample  `json:"samples"`
	Mismatches int       `json:"payload_mismatches"`
	Missing    int       `json:"unmatched_after_alignment"`
	First      time.Time `json:"-"`
	Offset     uint32    `json:"-"`
	Aligned    bool      `json:"-"`
	LastSeq    int       `json:"-"`
	Lost       int       `json:"lost_frames"`
	Reordered  int       `json:"duplicate_or_reordered_frames"`
}

// Hash only VCL NALs, preserving boundaries. MediaMTX may insert/remove SPS/PPS,
// AUD or SEI without changing the actual compressed picture.
func pictureHash(nalus [][]byte) [32]byte {
	h := sha256.New()
	var size [4]byte
	for _, n := range nalus {
		if len(n) > 0 && (n[0]&31 == 1 || n[0]&31 == 5) {
			binary.BigEndian.PutUint32(size[:], uint32(len(n)))
			h.Write(size[:])
			h.Write(n)
		}
	}
	var out [32]byte
	copy(out[:], h.Sum(nil))
	return out
}

func run(cfg config) (map[string]any, error) {
	if cfg.Seconds < 10 || cfg.Seconds > 300 || cfg.Readers < 1 || cfg.Readers > 8 {
		return nil, errors.New("invalid probe limits")
	}
	raw, err := os.ReadFile(cfg.Fixture)
	if err != nil {
		return nil, err
	}
	var f fixture
	if err = json.Unmarshal(raw, &f); err != nil {
		return nil, err
	}
	if len(f.SPS) == 0 || len(f.PPS) == 0 || f.LoopMS <= 0 || len(f.Packets) == 0 {
		return nil, errors.New("invalid fixture")
	}
	var ac mpeg4audio.AudioSpecificConfig
	if err = ac.Unmarshal(f.AudioConfig); err != nil {
		return nil, err
	}
	u, err := url.Parse(cfg.PublishURL)
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Duration(cfg.Seconds+30)*time.Second)
	defer cancel()
	pub := &gortmplib.Client{URL: u, Publish: true}
	if err = pub.Initialize(ctx); err != nil {
		return nil, fmt.Errorf("RTMP initialization: %w", err)
	}
	defer pub.Close()
	video := &gortmplib.Track{Codec: &codecs.H264{SPS: f.SPS, PPS: f.PPS}}
	audio := &gortmplib.Track{Codec: &codecs.MPEG4Audio{Config: &ac}}
	writer := &gortmplib.Writer{Conn: pub, Tracks: []*gortmplib.Track{video, audio}}
	if err = writer.Initialize(); err != nil {
		return nil, err
	}
	var mu sync.Mutex
	events := map[uint32]sent{}
	byHash := map[[32]byte][]uint32{}
	failed := make(chan error, 1)
	origin := time.Now()
	var sentVideo int
	// Start each timestamp immediately before protocol serialization/socket write.
	go func() {
		for loop := int64(0); ; loop++ {
			for _, p := range f.Packets {
				dts := time.Duration(loop*f.LoopMS+p.DTSMS) * time.Millisecond
				pts := time.Duration(loop*f.LoopMS+p.PTSMS) * time.Millisecond
				timer := time.NewTimer(time.Until(origin.Add(dts)))
				select {
				case <-ctx.Done():
					timer.Stop()
					return
				case <-timer.C:
				}
				if e := pub.NetConn().SetWriteDeadline(time.Now().Add(5 * time.Second)); e != nil {
					failed <- e
					return
				}
				if p.Video {
					tick := uint32(pts/time.Millisecond) * 90
					h := pictureHash(p.NALUs)
					at := time.Now()
					mu.Lock()
					events[tick] = sent{at: at, digest: h, tick: tick, seq: sentVideo}
					byHash[h] = append(byHash[h], tick)
					sentVideo++
					mu.Unlock()
					if e := writer.WriteH264(video, pts, dts, p.NALUs); e != nil {
						failed <- e
						return
					}
				} else if e := writer.WriteMPEG4Audio(audio, pts, p.Data); e != nil {
					failed <- e
					return
				}
			}
		}
	}()
	// The publisher must continue emitting while track detection finishes.
	var clients []*gortsplib.Client
	defer func() {
		cancel()
		for _, c := range clients {
			c.Close()
		}
	}()
	results := make([]*result, cfg.Readers)
	rurl, err := base.ParseURL(cfg.ReadURL)
	if err != nil {
		return nil, err
	}
	setupReader := func(index int, slow bool) error {
		transport := gortsplib.ProtocolTCP
		var c *gortsplib.Client
		var forma *format.H264
		for attempt := 0; attempt < 60; attempt++ {
			c = &gortsplib.Client{Scheme: rurl.Scheme, Host: rurl.Host, Protocol: &transport, ReadTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second}
			if e := c.Start(); e != nil {
				return e
			}
			desc, _, e := c.Describe(rurl)
			if e != nil {
				c.Close()
				select {
				case <-ctx.Done():
					return ctx.Err()
				case <-time.After(100 * time.Millisecond):
				}
				continue
			}
			medi := desc.FindFormat(&forma)
			if medi == nil {
				c.Close()
				return errors.New("H264 track missing")
			}
			dec, e := forma.CreateDecoder()
			if e != nil {
				c.Close()
				return e
			}
			if _, e = c.Setup(desc.BaseURL, medi, 0, 0); e != nil {
				c.Close()
				return e
			}
			rr := &result{}
			if !slow {
				results[index] = rr
			}
			c.OnPacketRTP(medi, forma, func(pkt *rtp.Packet) {
				if slow {
					select {
					case <-ctx.Done():
					case <-time.After(300 * time.Millisecond):
					}
					return
				}
				received := time.Now()
				au, e := dec.Decode(pkt)
				if e != nil {
					if e != rtph264.ErrMorePacketsNeeded && e != rtph264.ErrNonStartingPacketAndNoPrevious {
						mu.Lock()
						rr.Mismatches++
						mu.Unlock()
					}
					return
				}
				h := pictureHash(au)
				mu.Lock()
				defer mu.Unlock()
				if !rr.Aligned {
					var candidate sent
					count := 0
					for _, tick := range byHash[h] {
						s := events[tick]
						if received.Sub(s.at) < 2*time.Second {
							candidate = s
							count++
						}
					}
					if count != 1 {
						return
					}
					rr.Offset = pkt.Timestamp - candidate.tick
					rr.Aligned = true
					rr.First = received
				}
				event, ok := events[pkt.Timestamp-rr.Offset]
				if !ok {
					rr.Missing++
					return
				}
				if event.digest != h {
					rr.Mismatches++
					return
				}
				if received.Sub(rr.First) >= 2*time.Second {
					if event.seq <= rr.LastSeq {
						rr.Reordered++
					} else {
						rr.Lost += event.seq - rr.LastSeq - 1
					}
					rr.Samples = append(rr.Samples, sample{AtSeconds: received.Sub(origin).Seconds(), Milliseconds: received.Sub(event.at).Seconds() * 1000, Sequence: event.seq})
				}
				rr.LastSeq = event.seq
			})
			if _, e = c.Play(nil); e != nil {
				c.Close()
				return e
			}
			clients = append(clients, c)
			return nil
		}
		return errors.New("RTSP source readiness timeout")
	}
	for i := 0; i < cfg.Readers; i++ {
		if err = setupReader(i, false); err != nil {
			return nil, fmt.Errorf("RTSP reader: %w", err)
		}
	}
	if cfg.SlowReader {
		if err = setupReader(0, true); err != nil {
			return nil, err
		}
	}
	select {
	case err = <-failed:
		return nil, fmt.Errorf("RTMP write: %w", err)
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(time.Duration(cfg.Seconds+3) * time.Second):
	}
	mu.Lock()
	defer mu.Unlock()
	summaries := []map[string]any{}
	valid := true
	videoPerLoop := 0
	for _, p := range f.Packets {
		if p.Video {
			videoPerLoop++
		}
	}
	expectedFPS := float64(videoPerLoop) * 1000 / float64(f.LoopMS)
	for i, rr := range results {
		values := make([]float64, len(rr.Samples))
		for j, s := range rr.Samples {
			values[j] = s.Milliseconds
		}
		sort.Float64s(values)
		if len(values) < int(float64(cfg.Seconds)*expectedFPS*.9) {
			return nil, fmt.Errorf("reader %d insufficient matched samples: %d", i, len(values))
		}
		rate := float64(len(values)-1) / (rr.Samples[len(values)-1].AtSeconds - rr.Samples[0].AtSeconds)
		ok := rr.Mismatches == 0 && rr.Missing == 0 && rr.Lost == 0 && rr.Reordered == 0 && rate >= expectedFPS*.95 && values[0] >= 0
		valid = valid && ok
		summaries = append(summaries, map[string]any{"reader": i, "count": len(values), "min_ms": values[0], "median_ms": values[len(values)/2], "p95_ms": values[int(math.Ceil(float64(len(values))*.95))-1], "max_ms": values[len(values)-1], "received_fps": rate, "expected_fps": expectedFPS, "valid": ok, "payload_mismatches": rr.Mismatches, "unmatched_after_alignment": rr.Missing, "lost_frames": rr.Lost, "duplicate_or_reordered_frames": rr.Reordered, "samples": rr.Samples})
	}
	return map[string]any{"valid": valid, "readers": summaries, "slow_reader": cfg.SlowReader, "sent_video_frames": sentVideo, "scope": "RTMP send-before-write to complete matching H264 access unit over RTSP/TCP; includes protocol serialization, loopback/Docker network, MediaMTX, RTP depacketization; excludes media encoding, decoding and display", "matching": "RTP timestamp aligned once by unique SHA256 of VCL NALs; then timestamp and payload hash must both match", "audio": "AAC transmitted alongside video; audio delay not measured"}, nil
}

func main() {
	var cfg config
	if err := json.NewDecoder(os.Stdin).Decode(&cfg); err != nil {
		fmt.Fprintln(os.Stderr, "invalid input config")
		os.Exit(1)
	}
	report, err := run(cfg)
	if err != nil {
		msg := strings.ReplaceAll(err.Error(), cfg.PublishURL, "<publish-url>")
		msg = strings.ReplaceAll(msg, cfg.ReadURL, "<read-url>")
		fmt.Fprintln(os.Stderr, msg)
		os.Exit(1)
	}
	if err = json.NewEncoder(os.Stdout).Encode(report); err != nil {
		os.Exit(1)
	}
	if report["valid"] != true {
		os.Exit(2)
	}
}
