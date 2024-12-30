#!/usr/bin/env python3

import argparse
from time import sleep

from litex.tools.litex_client import RemoteClient


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csr-csv", default="csr.csv",     help="CSR configuration file")
    parser.add_argument("--host",    default="localhost",   help="Host ip address")
    parser.add_argument("--port",    default="1234",        help="Host bind port.")
    args = parser.parse_args()
    
    bus = RemoteClient(host=args.host, csr_csv=args.csr_csv, port=args.port)
    bus.open()

    # read SoC Ident string
    fpga_identifier = ""

    for i in range(256):
        c = chr(bus.read(bus.bases.identifier_mem + 4*i) & 0xff)
        fpga_identifier += c
        if c == "\0":
            break

    print(fpga_identifier)

    if False:
        for name, register in bus.regs.__dict__.items():
            print(f"{name} {register}")

    if False:
        print(bus.regs.ad9361_config.read())

    if True:
        # SPI reset / dump
        # from software/user/libm2sdr/m2sdr_ad9361_spi.c / m2sdr_ad9361_spi_init()
        bus.regs.ad9361_config.write(0)
        sleep(0.001)
        bus.regs.ad9361_config.write(2)
        sleep(0.001)

        # read 0x10 (data port configuration)
        # should read as 0xC0 initially
        reg = 0x10
        mosi = bytearray(4)
        mosi[0]  = (0 << 7);
        mosi[0] |= (reg >> 8) & 0x7f;
        mosi[1]  = (reg >> 0) & 0xff;
        mosi[2]  = 0x00;
        # write addr to mosi reg
        bus.regs.ad9361_spi_mosi.write((mosi[0] << 16) | (mosi[1] << 8) | (mosi[2]))

        # write length | start
        bus.regs.ad9361_spi_control.write( ((1 << 8) * 24) | 1 )

        # wait for done bit
        while bus.regs.ad9361_spi_status.read() != 1:
            print('spi ready spin')
            sleep(0.1)

        # read 
        reg_val = bus.regs.ad9361_spi_miso.read()
        print(f'reg {hex(reg)} value: {hex(reg_val)}')

    bus.close()
