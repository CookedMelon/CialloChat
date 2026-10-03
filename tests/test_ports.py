import socket
import unittest
from streamctl.diagnostics import check_ports


class Ports(unittest.TestCase):
    def test_real_listener_is_rejected(self):
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            occupied.bind(('127.0.0.1',0)); occupied.listen()
            port=occupied.getsockname()[1]
            with self.assertRaisesRegex(ValueError,'端口不可绑定'):
                check_ports({'mode':'local','rtsp_port':port,'api_port':port,'rtmp_port':port})

    def test_closed_listener_time_wait_allows_restart(self):
        server=socket.socket(); self.addCleanup(server.close)
        server.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        server.bind(('127.0.0.1',0)); server.listen()
        port=server.getsockname()[1]
        client=socket.create_connection(('127.0.0.1',port),timeout=2)
        self.addCleanup(client.close)
        accepted,_=server.accept()
        accepted.close(); self.assertEqual(client.recv(1),b'')
        client.close(); server.close()
        check_ports({'mode':'local','rtsp_port':port,'api_port':port,'rtmp_port':port})
