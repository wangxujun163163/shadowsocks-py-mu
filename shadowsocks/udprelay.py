#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# Copyright 2015 clowwindy
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# SOCKS5 UDP Request
# +----+------+------+----------+----------+----------+
# |RSV | FRAG | ATYP | DST.ADDR | DST.PORT |   DATA   |
# +----+------+------+----------+----------+----------+
# | 2  |  1   |  1   | Variable |    2     | Variable |
# +----+------+------+----------+----------+----------+

# SOCKS5 UDP Response
# +----+------+------+----------+----------+----------+
# |RSV | FRAG | ATYP | DST.ADDR | DST.PORT |   DATA   |
# +----+------+------+----------+----------+----------+
# | 2  |  1   |  1   | Variable |    2     | Variable |
# +----+------+------+----------+----------+----------+

# shadowsocks UDP Request (before encrypted)
# +------+----------+----------+----------+
# | ATYP | DST.ADDR | DST.PORT |   DATA   |
# +------+----------+----------+----------+
# |  1   | Variable |    2     | Variable |
# +------+----------+----------+----------+

# shadowsocks UDP Response (before encrypted)
# +------+----------+----------+----------+
# | ATYP | DST.ADDR | DST.PORT |   DATA   |
# +------+----------+----------+----------+
# |  1   | Variable |    2     | Variable |
# +------+----------+----------+----------+

# shadowsocks UDP Request and Response (after encrypted)
# +-------+--------------+
# |   IV  |    PAYLOAD   |
# +-------+--------------+
# | Fixed |   Variable   |
# +-------+--------------+

# HOW TO NAME THINGS
# ------------------
# `dest`    means destination server, which is from DST fields in the SOCKS5
#           request
# `local`   means local server of shadowsocks
# `remote`  means remote server of shadowsocks
# `client`  means UDP clients that connects to other servers
# `server`  means the UDP server that handles user requests

from __future__ import absolute_import, division, print_function, \
    with_statement

import sys
import socket
import logging
import struct
import errno
import random

from shadowsocks import cryptor, eventloop, lru_cache, common, shell
from shadowsocks.common import parse_header, pack_addr, onetimeauth_verify, \
    onetimeauth_gen, ONETIMEAUTH_BYTES, ADDRTYPE_AUTH


BUF_SIZE = 65536


def client_key(source_addr, server_af):
    # notice this is server af, not dest af
    return '%s:%s:%d' % (source_addr[0], source_addr[1], server_af)


class UDPRelay(object):

    def __init__(self, config, dns_resolver, is_local, stat_callback=None):
        self._config = config
        if is_local:
            self._listen_addr = config['local_address']
            self._listen_port = config['local_port']
            self._remote_addr = config['server']
            self._remote_port = config['server_port']
        else:
            self._listen_addr = config['server']
            self._listen_port = config['server_port']
            self._remote_addr = None
            self._remote_port = None
        self._dns_resolver = dns_resolver
        self._password = common.to_bytes(config['password'])
        self._method = config['method']
        self._timeout = config['timeout']
        if 'one_time_auth' in config:
            self._one_time_auth_enable = config['one_time_auth']
        else:
            self._one_time_auth_enable = False
        self._is_local = is_local
        self._cache = lru_cache.LRUCache(timeout=config['timeout'],
                                         close_callback=self._close_client)
        self._client_fd_to_server_addr = \
            lru_cache.LRUCache(timeout=config['timeout'])
        self._dns_cache = lru_cache.LRUCache(timeout=300)
        self._eventloop = None
        self._closed = False
        self._sockets = set()
        if 'forbidden_ip' in config:
            self._forbidden_iplist = config['forbidden_ip']
        else:
            self._forbidden_iplist = None

        addrs = socket.getaddrinfo(self._listen_addr, self._listen_port, 0,
                                   socket.SOCK_DGRAM, socket.SOL_UDP)
        if len(addrs) == 0:
            raise Exception("UDP getaddrinfo failed for %s:%d" %
                            (self._listen_addr, self._listen_port))
        af, socktype, proto, canonname, sa = addrs[0]
        server_socket = socket.socket(af, socktype, proto)
        server_socket.bind((self._listen_addr, self._listen_port))
        server_socket.setblocking(False)
        self._server_socket = server_socket
        self._stat_callback = stat_callback

    def _get_a_server(self):
        server = self._config['server']
        server_port = self._config['server_port']
        if type(server_port) == list:
            server_port = random.choice(server_port)
        if type(server) == list:
            server = random.choice(server)
        logging.debug('U[%d] UDP Chosen server: %s:%d' %
                      (self._config['server_port'], server, server_port))
        return server, server_port

    def _close_client(self, client):
        if hasattr(client, 'close'):
            self._sockets.remove(client.fileno())
            self._eventloop.remove(client)
            client.close()
        else:
            # just an address
            pass

    def _handle_server(self):
        server = self._server_socket
        data, r_addr = server.recvfrom(BUF_SIZE)
        client_address = r_addr[0]
        key = None
        iv = None
        if not data:
            logging.debug('U[%d] UDP handle_server: data is empty' %
                          self._config['server_port'])
        if self._stat_callback:
            self._stat_callback(self._listen_port, len(data))
        if self._is_local:
            frag = common.ord(data[2])
            if frag != 0:
                logging.warn('U[%d] UDP drop a message since frag is not 0' % self._config[
                             'server_port'])
                return
            else:
                data = data[3:]
        else:
            data, key, iv = cryptor.decrypt_all(self._password,
                                                self._method,
                                                data)
            # decrypt data
            if not data:
                logging.debug(
                    'U[%d] UDP handle_server: data is empty after decrypt' % self._config[
                        'server_port']
                )
                return
        header_result = parse_header(data)
        if header_result is None:
            return
        addrtype, dest_addr, dest_port, header_length = header_result

        if self._config['firewall_ports'] and self._config['server_port'] not in self._config['firewall_trusted']:
            # Firewall enabled
            if self._config['firewall_mode'] == 'blacklist' and dest_port in self._config['firewall_ports']:
                firewall_blocked = True
            elif self._config['firewall_mode'] == 'whitelist' and dest_port not in self._config['firewall_ports']:
                firewall_blocked = True
            else:
                firewall_blocked = False
        else:
            firewall_blocked = False
        if firewall_blocked:
            logging.warning('U[%d] UDP PORT BANNED: RP[%d] A[%s-->%s]' % (
                self._config['server_port'], dest_port,
                client_address, common.to_str(dest_addr)
            ))
            return
        else:
            logging.info('U[%d] UDP CONN: RP[%d] A[%s-->%s]' % (
                self._config['server_port'], dest_port,
                client_address, common.to_str(dest_addr)
            ))
        if self._is_local:
            server_addr, server_port = self._get_a_server()
        else:
            server_addr, server_port = dest_addr, dest_port
            # spec https://shadowsocks.org/en/spec/one-time-auth.html
            if self._one_time_auth_enable or (addrtype & ADDRTYPE_AUTH == ADDRTYPE_AUTH):
                if not self._one_time_auth_enable and self._config['verbose']:
                        logging.info('U[%d] UDP one time auth automatically enabled' % self._config['server_port'])
                self._one_time_auth_enable = True
                if len(data) < header_length + ONETIMEAUTH_BYTES:
                    logging.warn('U[%d] UDP one time auth header is too short' % self._config[
                                 'server_port'])
                    return
                _hash = data[-ONETIMEAUTH_BYTES:]
                data = data[: -ONETIMEAUTH_BYTES]
                _key = iv + key
                if onetimeauth_verify(_hash, data, _key) is False:
                    logging.warn('U[%d] UDP one time auth fail' %
                                 self._config['server_port'])
                    return
        addrs = self._dns_cache.get(server_addr, None)
        if addrs is None:
            addrs = socket.getaddrinfo(server_addr, server_port, 0,
                                       socket.SOCK_DGRAM, socket.SOL_UDP)
            if not addrs:
                # drop
                return
            else:
                self._dns_cache[server_addr] = addrs

        af, socktype, proto, canonname, sa = addrs[0]
        key = client_key(r_addr, af)
        client = self._cache.get(key, None)
        if not client:
            # TODO async getaddrinfo
            if self._forbidden_iplist:
                if common.to_str(sa[0]) in self._forbidden_iplist:
                    logging.debug('U[%d] IP %s is in forbidden list, drop' %
                                  (self._config['server_port'], common.to_str(sa[0])))
                    # drop
                    return
            client = socket.socket(af, socktype, proto)
            client.setblocking(False)
            self._cache[key] = client
            self._client_fd_to_server_addr[client.fileno()] = r_addr

            self._sockets.add(client.fileno())
            self._eventloop.add(client, eventloop.POLL_IN, self)

        if self._is_local:
            key, iv, m = cryptor.gen_key_iv(self._password, self._method)
            # spec https://shadowsocks.org/en/spec/one-time-auth.html
            if self._one_time_auth_enable:
                data = self._ota_chunk_data_gen(key, iv, data)
            data = cryptor.encrypt_all_m(key, iv, m, self._method, data)
            if not data:
                return
        else:
            data = data[header_length:]
        if not data:
            return
        try:
            client.sendto(data, (server_addr, server_port))
        except (socket.error, OSError, IOError) as e:
            error_no = eventloop.errno_from_exception(e)
            if sys.platform == "win32":
                if error_no in (errno.EAGAIN, errno.EINPROGRESS,
                                errno.EWOULDBLOCK, errno.WSAEWOULDBLOCK):
                    pass
                else:
                    shell.print_exception(e)
            elif error_no in (errno.EAGAIN, errno.EINPROGRESS,
                              errno.EWOULDBLOCK):
                pass
            else:
                shell.print_exception(e)

    def _handle_client(self, sock):
        data, r_addr = sock.recvfrom(BUF_SIZE)
        if not data:
            logging.debug('U[%d] UDP handle_client: data is empty' %
                          self._config['server_port'])
            return
        if self._stat_callback:
            self._stat_callback(self._listen_port, len(data))
        if not self._is_local:
            addrlen = len(r_addr[0])
            if addrlen > 255:
                # drop
                return
            data = pack_addr(r_addr[0]) + struct.pack('>H', r_addr[1]) + data
            response = cryptor.encrypt_all(self._password, self._method, data)
            if not response:
                return
        else:
            data, key, iv = cryptor.decrypt_all(self._password,
                                                self._method, data)
            if not data:
                return
            if parse_header(data) is None:
                return
            response = b'\x00\x00\x00' + data
        client_addr = self._client_fd_to_server_addr.get(sock.fileno())
        if client_addr:
            self._server_socket.sendto(response, client_addr)
        else:
            # this packet is from somewhere else we know
            # simply drop that packet
            pass

    @staticmethod
    def _ota_chunk_data_gen(key, iv, data):
        data = common.chr(common.ord(data[0]) | ADDRTYPE_AUTH) + data[1:]
        key += iv
        return data + onetimeauth_gen(data, key)

    def add_to_loop(self, loop):
        if self._eventloop:
            raise Exception('already add to loop')
        if self._closed:
            raise Exception('already closed')
        self._eventloop = loop

        server_socket = self._server_socket
        self._eventloop.add(server_socket,
                            eventloop.POLL_IN | eventloop.POLL_ERR, self)
        loop.add_periodic(self.handle_periodic)

    def handle_event(self, sock, fd, event):
        if sock == self._server_socket:
            if event & eventloop.POLL_ERR:
                logging.error('U[%d] UDP server_socket err' %
                              self._config['server_port'])
            self._handle_server()
        elif sock and (fd in self._sockets):
            if event & eventloop.POLL_ERR:
                logging.error('U[%d] UDP client_socket err' %
                              self._config['server_port'])
            self._handle_client(sock)

    def handle_periodic(self):
        if self._closed:
            if self._server_socket:
                self._server_socket.close()
                self._server_socket = None
                for sock in self._sockets:
                    sock.close()
                logging.info('U[%d] UDP port %d closed' %
                             (self._config['server_port'], self._listen_port))
        self._cache.sweep()
        self._client_fd_to_server_addr.sweep()
        self._dns_cache.sweep()

    def close(self, next_tick=False):
        logging.debug('U[%d] UDP closed' % self._config['server_port'])
        self._closed = True
        if not next_tick:
            if self._eventloop:
                self._eventloop.remove_periodic(self.handle_periodic)
                self._eventloop.remove(self._server_socket)
            self._server_socket.close()
            for client in list(self._cache.values()):
                client.close()
