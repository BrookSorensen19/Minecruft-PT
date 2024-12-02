#!/usr/bin/env python
"""
Usage:
    Minecruft_Main.py client <aes_keyfile> <dest_ip> <dest_port>
                      <fwd_ports> <encoder_actions_file> <decoder_actions_file> <hmm_file>[options]
    Minecruft_Main.py proxy <aes_keyfile> <dest_ip> <dest_port>
                      <fwd_ports>  <encoder_actions_file>
                      <decoder_actions_file> <hmm_file>[options]

    Minecruft_Main.py <config>

Options:
    -h --help                  Show help message
    --proxy_port <proxy_port>  Proxy Listen Address [default: 25566]
    -t --test                  Send test message through Minecruft instead of TCP packets
    --encoder_weights_file <weights>    Encoder Weights
"""

import argparse
import multiprocessing
import json
import time
#from docopt import docopt
import logging
import hashlib

#Minecraft Bot Libraries - Twisted does the event handling
from twisted.internet import reactor, defer
from quarry.types.buffer import Buffer1_7
from quarry.net.auth import ProfileCLI

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

#Scapy does the packet sniffing
from scapy.all import sniff, L3RawSocket  # pylint: disable=unused-import 
from scapy.layers.inet import IP, TCP

from MinecraftClientEncoder import MinecraftEncoderFactory
from MinecraftProxyEncoder import MinecraftProxyFactory

def filter_packets(incoming_tcp_q, duplicate_packets):
    """Factory Function for generating filters"""
    def send_filtered_packets(packet):
        """
        Function for filtering out nonTCP packets and saving the TCP packets into the
        incoming_tcp_q.
        """
        data = packet['TCP']
        if not data in duplicate_packets:
            incoming_tcp_q.put(data)
            duplicate_packets.append(data)
        else:
            duplicate_packets.remove(data)
    return send_filtered_packets

def receive_tcp_data(tcp_port, direction, incoming_tcp_q):
    """
    This is a function for sending packets from the host.

    Sniffs all packets of localhost that have ports with the value tcp_port traveling
    either from src or dst using the direction variable.

    direction: src or dst
    tcp_port: String resembling TCP ports such as 9000, 20, 80
    incoming_tcp_q: Queue for saving incoming packets
    """
    port_str_lst = []
    for port in tcp_port.split(','):
        port_str_lst.append('tcp ' + direction + ' port ' +  port)
    port_str = ' or '.join(port_str_lst)

    duplicate_packets = []
    filt = "host 127.0.0.1 and ( " + port_str + " )"
    sniff(filter=filt, prn=filter_packets(incoming_tcp_q, duplicate_packets), iface="lo")
 
def breceive_tcp_data(tcp_port, direction, incoming_tcp_q):
    """
    This is a function for sending packets from the host.

    Sniffs all packets of localhost that have ports with the value tcp_port traveling
    either from src or dst using the direction variable.

    direction: src or dst
    tcp_port: String resembling TCP ports such as 9000, 20, 80
    incoming_tcp_q: Queue for saving incoming packets
    """
    #port_str_lst = []
    #for port in tcp_port.split(','):
    #    port_str_lst.append('tcp ' + direction + ' port ' +  port)
    #port_str = ' or '.join(port_str_lst)
#
#    duplicate_packets = []
#    filt = "host 127.0.0.1 and ( " + port_str + " )"
#    sniff(filter=filt, prn=filter_packets(incoming_tcp_q, duplicate_packets), iface="lo")
    sock = L3RawSocket(iface="lo")
    tcp_port = int(tcp_port)
    duplicate_packets = []
    while True: 
        data = sock.recv()
        test = False 
        if TCP in data:
            data = data["TCP"]
            if direction == "dst": 
                test = (data.dport == tcp_port) 
            elif direction == "src": 
                test = (data.sport == tcp_port) 
        if test:
            stripped_pack = TCP(ack=data.ack, seq=data.seq, flags=data.flags)/data.payload
            #logging.warning(bytes(stripped_pack))
            if not hash(bytes(stripped_pack)) in duplicate_packets:
                if not incoming_tcp_q.full(): 
                    #logging.warning("Sending: ")
                    #logging.warning(data.summary())
                    logging.info("Adding to queue " + data.summary())
                    logging.info(f"Ack {data.ack}, Seq {data.seq}, flags {data.flags}, payload length {len(data.payload)},\n options {data.options}")
                    incoming_tcp_q.put(data)
                    logging.info("Queue has " + str(incoming_tcp_q.qsize()))
                duplicate_packets.append(hash(bytes(stripped_pack)))
            elif len(duplicate_packets) > 100:
                duplicate_packets.pop(0)

def encrypt_tcp_data(incoming_tcp_q, encrypt_tcp_q, direction, key):
    """
    This is a function for sending packets from the host.

    Removes packets from incoming_tcp_q, encrypts them with AES_ECB, and then stores the
    encrypted packets into the encrypt_tcp_q.
    """
    while True:
        if not incoming_tcp_q.empty():
            raw_data = incoming_tcp_q.get()
            padded_block = pad(bytes(raw_data), AES.block_size)
            encrypt_blocks = encrypt_load(padded_block, key)
            block_hash = hashlib.sha1(encrypt_blocks)
            #logging.warning(block_hash.digest())
            encrypt_blocks = encrypt_blocks + block_hash.digest()
            encrypt_tcp_q.put(bytearray(encrypt_blocks))
        
def sim_tcp_data(incoming_tcp_q, encrypt_tcp_q, direction, key): 
    while True: 
        time.sleep(1)
        encrypt_tcp_q.put(bytearray(b"The test is best"))

def decrypt_enc_data(decrypt_tcp_q, response_q, key):
    """
    This is a function for receiving packets on the host.

    Removes packets from decrypt_tcp_q, decrypts them with AES_ECB, and then stores the
    encrypted packets into the reponse_q.
    """ 
    while True:
        enc_pack = decrypt_tcp_q.get()
        if enc_pack != None and len(enc_pack) > 0:
            
            cmp_hash = enc_pack[-20:]  
            enc_pack = enc_pack[:-20]
            #logging.warning(cmp_hash)
            if cmp_hash == hashlib.sha1(enc_pack).digest(): 
                decrypted_pack = decrypt_load(enc_pack, key)
                unpadded_pack = unpad(decrypted_pack, AES.block_size)
                response_q.put(bytes(unpadded_pack))
            #except ValueError as e:
            #    logging.warn(f"Error {e}")
            #    pass

@defer.inlineCallbacks #Needed due to twisted's event handling
def runargs(encoder_actions, decoder_actions, args, encrypt_tcp_q, decrypt_tcp_q, encoder_weights, hmm_file):
    """
    Creates a Minecraft Client Bot, and connects to the Minecraft proxy.
    This assumes that the Minecraft Proxy is already running on another machine.
    """
    profile = yield ProfileCLI.make_profile(args)
    factory = MinecraftEncoderFactory(encoder_actions, decoder_actions, profile, encrypt_tcp_q, decrypt_tcp_q, encoder_weights, hmm_file)
    factory.connect(args.host, args.port)

def client_forward_packet(encrypt_tcp_q, decrypt_tcp_q, encoder_actions, 
                          decoder_actions, forward_addr, proxy_port, 
                          encoder_weights, hmm_file):
    """
    Starts twisted's event listener for sending and recieving minecraft packets,
    and sets up the client bot to connect to the proper host machine using the
    runargs function. Also passes the encrypt_tcp_q (for forwarding encrypted
    packets) and the decrypt_tcp_q (for receiving encrypted packets) to the
    minecraft client encoder object.
    """
    parser = ProfileCLI.make_parser()
    parser.add_argument("host")
    parser.add_argument("-p", "--port", default=25566, type=int)

    #Later take input and/or pick from a name in a database
    myarr = [forward_addr, "-p", str(proxy_port), "--offline-name", "Bot"]
    args = parser.parse_args(myarr)
    runargs(encoder_actions, decoder_actions, args, encrypt_tcp_q, decrypt_tcp_q, encoder_weights, hmm_file)
    reactor.run() #pylint: disable=no-member

def proxy_forward_packet(encrypt_tcp_q, decrypt_tcp_q, downstream_encoder_actions, 
                         upstream_decoder_actions, minecraft_server_addr, minecraft_server_port,
                         listen_addr, listen_port, encoder_weights, hmm_file):
    """
    Starts twisted's event listener for sending and recieving minecraft packets,
    and sets up the proxy server which first connects to a Minecraft server set in offline
    mode and then waits for incoming Minecraft
    Client connections. Also passes the encrypt_tcp_q (for forwarding encrypted
    packets) and the decrypt_tcp_q (for receiving encrypted packets) to the
    minecraftProxyEncoder object.
    """
    factory = MinecraftProxyFactory(encoder_actions, decoder_actions, encrypt_tcp_q, decrypt_tcp_q, encoder_weights, hmm_file)
    factory.online_mode = False
    factory.force_protocol_version = 340
    factory.connect_host = minecraft_server_addr
    factory.connect_port = minecraft_server_port
    print(listen_addr, listen_port)
    factory.listen(listen_addr, listen_port)
    reactor.run() #pylint: disable=no-member

def inject_tcp_packets(response_q):
    """
    Takes the received unencrypted response_q TCP packets and injects them
    onto the localhost's machine.
    """
    sock = L3RawSocket(iface="lo")
    while 1:
        if response_q.qsize() > 0:
            b_pkt = response_q.get()
            pkt = TCP(b_pkt)
            tcp = IP(dst='127.0.0.1')/pkt['TCP']

            #print("Injected")
            del tcp['TCP'].chksum
            sock.send(tcp)

def encrypt_load(message, key):
    """
    Encrypts with AES_ECB using a default password. This is not for security
    it is for creating an even chance of sending binary ones and zeros.
    """
    cryptr = AES.new(key, AES.MODE_ECB)
    cipher_str = cryptr.encrypt(message)
    return cipher_str

def decrypt_load(cipher_str, key):
    """
    Encrypts with AES_ECB using a default password. This is not for security
    it is for creating an even chance of sending binary ones and zeros.
    """
    cryptr = AES.new(key, AES.MODE_ECB)
    message = cryptr.decrypt(cipher_str)
    return message
"""
if __name__ == '__main__':
    #Parse input arguments, require mode, dest_ip, dest_port, and fwd_ports.
    #logging.basicConfig(level=30, format="%(message)s")
    pargs = docopt(__doc__) 
    print(pargs)

    if "<config>" in pargs: 
        conffile = pargs["<config>"]
        with open(conffile, "r") as conffp:
            pargs = json.load(conffp)

    hmm_file = pargs["<hmm_file>"]
    #Initialize and set important vaiables
    ports = pargs["<fwd_ports>"]
    dest_port = int(pargs["<dest_port>"])
    dest_ip = pargs["<dest_ip>"]

    encoder_actions_file =  pargs["<encoder_actions_file>"]
    decoder_actions_file =  pargs["<decoder_actions_file>"]

    encoder_weights_file =  pargs["--encoder_weights_file"]

    aes_keyfile = pargs["<aes_keyfile>"] 
    with open(aes_keyfile, "rb") as f: 
        aes_key =  f.read() 

    decoder_actions = {}
    encoder_actions = {}

    with open(decoder_actions_file) as f: 
        decoder_actions =  json.load(f)

    with open(encoder_actions_file) as f: 
        encoder_actions =  json.load(f)

    encoder_weights = None
    if encoder_weights_file:
        with open(encoder_weights_file) as f: 
            encoder_weights = json.load(f)

    #These queues are used throughout the project
    sniffed_packets_queue = multiprocessing.Queue()
    encrypt_queue = multiprocessing.Queue()
    decrypt_queue = multiprocessing.Queue()
    response_queue = multiprocessing.Queue()
    fte_func_args = ()
    packetFlag = True

    fwd_addr = (dest_ip, dest_port)

    #Client and Server specific setup and function choices
    #A Client will run a MinecraftClientEncoder and a Server
    #will run a proxy.
    if pargs["client"]:
        direction = "dst"
        forward_packet = client_forward_packet
        fte_func_args = (encrypt_queue, decrypt_queue, encoder_actions, 
                         decoder_actions, dest_ip, dest_port, encoder_weights, hmm_file)
    elif pargs["proxy"]:
        direction = "src"
        proxy_port = int(pargs["--proxy_port"])
        forward_packet = proxy_forward_packet
        fte_func_args = (encrypt_queue, decrypt_queue, encoder_actions, decoder_actions,
                         dest_ip, dest_port, "0.0.0.0", proxy_port, encoder_weights, hmm_file) 
    else:
        exit()

    if pargs["--test"]:
        print("Starting Test")
        process_functions = {receive_tcp_data: (ports, direction, sniffed_packets_queue), 
                        sim_tcp_data: (sniffed_packets_queue, encrypt_queue, direction, aes_key), 
                        forward_packet: fte_func_args } 
    else: 
        process_functions = {receive_tcp_data: (ports, direction, sniffed_packets_queue), 
                        encrypt_tcp_data: (sniffed_packets_queue, encrypt_queue, direction, aes_key), 
                        forward_packet: fte_func_args,
                        decrypt_enc_data: (decrypt_queue, response_queue, aes_key),
                        inject_tcp_packets: (response_queue,) } 
 
    #Start the processes
    try:
        print("Ready")
        process_list = [multiprocessing.Process(target=k, args=v) 
                        for k,v in process_functions.items()]

        for process in process_list:
            process.start()
        
        for process in process_list: 
            process.join()
            
    except KeyboardInterrupt:
        print("Keyboard Interrupt - Stopping server")

        for process in process_list: 
            process.terminate()
    """

if __name__ == '__main__':
    # Set up the argument parser
    parser = argparse.ArgumentParser(description="Minecruft Main Script")
    parser.add_argument("mode", choices=["client", "proxy", "<config>"], help="Mode of operation (client, proxy, or config)")
    parser.add_argument("aes_keyfile", help="Path to AES key file")
    parser.add_argument("dest_ip", help="Destination IP address")
    parser.add_argument("dest_port", type=int, help="Destination port")
    parser.add_argument("fwd_ports", help="Comma-separated list of forwarding ports")
    parser.add_argument("encoder_actions_file", help="Path to encoder actions file")
    parser.add_argument("decoder_actions_file", help="Path to decoder actions file")
    parser.add_argument("hmm_file", help="Path to HMM file")
    parser.add_argument("-t", "--test", action="store_true", help="Send test message through Minecruft")
    parser.add_argument("--proxy_port", type=int, default=25566, help="Proxy listen address (default: 25566)")
    parser.add_argument("--encoder_weights_file", help="Encoder weights file")

    args = parser.parse_args()

    if args.mode == "<config>":
        with open(args.config, "r") as conffp:
            args = json.load(conffp)

    hmm_file = args.hmm_file
    ports = args.fwd_ports
    dest_port = args.dest_port
    dest_ip = args.dest_ip
    encoder_actions_file = args.encoder_actions_file
    decoder_actions_file = args.decoder_actions_file
    encoder_weights_file = args.encoder_weights_file

    aes_keyfile = args.aes_keyfile
    with open(aes_keyfile, "rb") as f:
        aes_key = f.read()

    decoder_actions = {}
    encoder_actions = {}

    with open(decoder_actions_file) as f:
        decoder_actions = json.load(f)

    with open(encoder_actions_file) as f:
        encoder_actions = json.load(f)

    encoder_weights = None
    if encoder_weights_file:
        with open(encoder_weights_file) as f:
            encoder_weights = json.load(f)

    # Initialize queues
    sniffed_packets_queue = multiprocessing.Queue()
    encrypt_queue = multiprocessing.Queue()
    decrypt_queue = multiprocessing.Queue()
    response_queue = multiprocessing.Queue()

    fwd_addr = (dest_ip, dest_port)

    # Client and Server specific setup
    if args.mode == "client":
        direction = "dst"
        forward_packet = client_forward_packet
        fte_func_args = (encrypt_queue, decrypt_queue, encoder_actions, 
                         decoder_actions, dest_ip, dest_port, encoder_weights, hmm_file)
    elif args.mode == "proxy":
        direction = "src"
        proxy_port = args.proxy_port
        forward_packet = proxy_forward_packet
        fte_func_args = (encrypt_queue, decrypt_queue, encoder_actions, decoder_actions,
                         dest_ip, dest_port, "0.0.0.0", proxy_port, encoder_weights, hmm_file)
    else:
        exit()

    if args.test:
        print("Starting Test")
        process_functions = {receive_tcp_data: (ports, direction, sniffed_packets_queue), 
                             sim_tcp_data: (sniffed_packets_queue, encrypt_queue, direction, aes_key), 
                             forward_packet: fte_func_args}
    else:
        process_functions = {receive_tcp_data: (ports, direction, sniffed_packets_queue), 
                             encrypt_tcp_data: (sniffed_packets_queue, encrypt_queue, direction, aes_key), 
                             forward_packet: fte_func_args,
