from typing import List

samplesA = [
    # off
    # bytearray(b'\x01\x01\x01]\x00\x03\xda,'),
    # bytearray(b'\x01\x01\x01k\x00\x03\xe2L'),
    # bytearray(b'\x01\x01\x01v\x00\x03\xe3P'),
    # bytearray(b'\x01\x01\x01\x80\x00\x03\xe3P'),

    # bytearray(b'\x00\x00\x00\x9f\x00\x03\xf7\xa0'),
    # bytearray(b'\x01\x01\x01\xc0\x00\x03\xf7\xa0'),
    # bytearray(b'\x00\x00\x00\xf7\x00\x03\xf7\xa0'),
    # bytearray(b'\x00\x00\x00\x06\x00\x03\xf7\xa0'),

    #bytearray(b'\x01\x13\x00\x00u0\x03\xe8'),
    #bytearray(b'\x01\x12\x00\x00u0\x03\xe8'),
    #bytearray(b'\x01\x12\x00\x00u0\x03\xe8'),

    [ D2 03 04 00 00 00 00 18 FE ],
    D203 0400 0000 0018 FE
    D203 0400 0000 0018 FE
    D203 0400 0000 0018 FE


"""
on
Nov 12 13:46:21.626  ATT Receive      0x0055  00:00:00:00:00:00  Handle Value Notification - Handle:0x0012 - Value: D203 0414 0306 00EE AE  

Nov 12 13:47:04.511  ATT Receive      0x0055  00:00:00:00:00:00  Handle Value Notification - Handle:0x0012 - Value: D206 00A6 0001 BB8A  


off
Nov 12 13:46:46.824  ATT Receive      0x0055  00:00:00:00:00:00  Handle Value Notification - Handle:0x0012 - Value: D206 00A6 0000 7A4A  



turn dsg on:
Nov 12 13:49:07.061  ATT Send         0x0055  46:64:01:02:04:8E  Write Command - Handle:0x0010 - FFF2 - Value: D206 00A6 0001 BB8A  
Nov 12 13:49:07.228  ATT Receive      0x0055  46:64:01:02:04:8E  Handle Value Notification - Handle:0x0012 - FFF1 - Value: D206 00A6 0001 BB8A  

turn dsg off
Nov 12 13:49:52.712  ATT Send         0x0055  46:64:01:02:04:8E  Write Command - Handle:0x0010 - FFF2 - Value: D206 00A6 0000 7A4A  
Nov 12 13:49:52.857  ATT Receive      0x0055  46:64:01:02:04:8E  Handle Value Notification - Handle:0x0012 - FFF1 - Value: D206 00A6 0000 7A4A  




"""




]

samplesB = [
    # on
    # bytearray(b'\x01\x01\x017\x00\x03\xf7\xa0'),
    # bytearray(b'\x01\x01\x01E\x00\x03\xf7\xa0'),
    # bytearray(b'\x02\x00\x01R\x00\x03\xf7\xa0'),

   # bytearray(b'\x01\x13\x00\x00u0\x03\xe8'),
   # bytearray(b'\x01\x11\x00\x00u0\x03\xe8'),
   # bytearray(b'\x01\x15\x00\x00ts\x03\xe8'),

    # bytearray(b'\x01\x01\x01\xca\x00\x03\xdd8'),
    # bytearray(b'\x01\x01\x01\xf0\x00\x03\xdf@'),
    # bytearray(b'\x01\x01\x01\x15\x00\x03\xe0D'),
    # bytearray(b'\x01\x01\x01!\x00\x03\xe0D'),
]


def access_bit(data, num):
    base = int(num // 8)
    shift = int(num % 8)
    return (data[base] >> shift) & 0x1


bitsA = [[access_bit(s, i) for i in range(len(s) * 8)] for s in samplesA]
bitsB = [[access_bit(s, i) for i in range(len(s) * 8)] for s in samplesB]

import pandas as pd


def find_const_bits(bits: List[List[int]]):
    df = pd.DataFrame(bits)
    bs = df.sum(axis=0)
    return (bs == 0) | (bs == len(bits))


constA = find_const_bits(bitsA)
constB = find_const_bits(bitsB)

for i, c in constA.items():
    if c and constB[i] and bitsA[0][i] != bitsB[0][i]:
        print("bit %d is const in both groups and changes (A=%d, B=%d)" % (i, bitsA[0][i], bitsB[0][i]))
