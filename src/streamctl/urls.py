"""Public media addresses, independent of the backend listening sockets."""


def public_urls(settings):
    scheme = 'rtmps' if settings['mode'] == 'production' else 'rtmp'
    host = settings['hostname'] if settings['mode'] == 'production' or settings.get('local_network') else '127.0.0.1'
    proxied = settings.get('reverse_proxy_enabled', False)
    read_host = settings.get('read_hostname', host) if settings['mode'] == 'production' else host
    publish_port = (settings.get('public_rtmps_port', 443) if proxied else
                    settings['rtmps_port' if scheme == 'rtmps' else 'rtmp_port'])
    read_port = settings.get('public_rtsp_port', 554) if proxied else settings['rtsp_port']

    def authority(name, port, default):
        name = '[' + name + ']' if ':' in name else name
        return name if proxied and port == default else f'{name}:{port}'

    publish = f'{scheme}://' + authority(host, publish_port, 443)
    read = 'rtsp://' + authority(read_host, read_port, 554)
    return dict(publish_base=publish, read_base=read,
                test_url=read + '/test' if settings.get('test_video_enabled') else None)
