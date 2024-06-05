#!/usr/bin/env python3

#
# This file is part of LiteX-M2SDR.
#
# Copyright (c) 2024 Enjoy-Digital <enjoy-digital.fr>
# SPDX-License-Identifier: BSD-2-Clause

import os
import time
import argparse

from migen import *

from litex.gen import *

from litex.build.generic_platform import Subsignal, Pins
from litex_m2sdr_platform import Platform

from litex.soc.interconnect.csr import *
from litex.soc.interconnect     import stream

from litex.soc.integration.soc_core import *
from litex.soc.integration.builder  import *

from litex.soc.cores.clock     import *
from litex.soc.cores.led       import LedChaser
from litex.soc.cores.icap      import ICAP
from litex.soc.cores.xadc      import XADC
from litex.soc.cores.dna       import DNA
from litex.soc.cores.pwm       import PWM
from litex.soc.cores.bitbang   import I2CMaster
from litex.soc.cores.gpio      import GPIOOut
from litex.soc.cores.spi_flash import S7SPIFlash

from litex.build.generic_platform import IOStandard, Subsignal, Pins

from litepcie.phy.s7pciephy import S7PCIEPHY

from liteeth.phy.a7_gtp       import QPLLSettings, QPLL
from liteeth.phy.a7_1000basex import A7_1000BASEX

from litesata.phy import LiteSATAPHY

from litescope import LiteScopeAnalyzer

from gateware.ad9361.core import AD9361RFIC
from gateware.timestamp   import Timestamp
from gateware.header      import TXRXHeader
from gateware.measurement import MultiClkMeasurement

from software import generate_litepcie_software
from software import get_pcie_device_id, remove_pcie_device, rescan_pcie_bus

# CRG ----------------------------------------------------------------------------------------------

class CRG(LiteXModule):
    def __init__(self, platform, sys_clk_freq, with_ethernet=False, with_sata=False):
        self.cd_sys      = ClockDomain()
        self.cd_idelay   = ClockDomain()
        self.cd_eth_ref  = ClockDomain()
        self.cd_sata_ref = ClockDomain()

        # # #

        # Clk / Rst.
        clk100 = platform.request("clk100")

        # PLL.
        self.pll = pll = S7PLL(speedgrade=-3)
        pll.register_clkin(clk100, 100e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        pll.create_clkout(self.cd_idelay, 200e6)

        # IDelayCtrl.
        self.idelayctrl = S7IDELAYCTRL(self.cd_idelay)

        # Ethernet PLL.
        if with_ethernet or with_sata:
            self.eth_pll = eth_pll = S7PLL()
            eth_pll.register_clkin(self.cd_sys.clk, sys_clk_freq)
            eth_pll.create_clkout(self.cd_eth_ref, 156.25e6, margin=0)

        # SATA PLL.
        if with_sata or with_ethernet:
            self.sata_pll = sata_pll = S7PLL()
            sata_pll.register_clkin(self.cd_sys.clk, sys_clk_freq)
            sata_pll.create_clkout(self.cd_sata_ref, 150e6, margin=0)

# BaseSoC -----------------------------------------------------------------------------------------

class BaseSoC(SoCMini):
    SoCCore.csr_map = {
        # SoC.
        "ctrl"        : 0,
        "uart"        : 1,
        "icap"        : 2,
        "flash"       : 3,
        "xadc"        : 4,
        "dna"         : 5,
        "flash"       : 6,
        "leds"        : 7,

        # PCIe.
        "pcie_phy"    : 10,
        "pcie_msi"    : 11,
        "pcie_dma0"   : 12,

        # Eth.
        "eth_phy"     : 14,

        # SATA.
        "sata_phy"    : 15,
        "sata_core"   : 16,

        # SDR.
        "si5351_i2c"  : 20,
        "si5351_pwm"  : 21,
        "timestamp"   : 22,
        "header"      : 23,
        "ad9361"      : 24,

        # Measurements/Analyzer.
        "clk_measurement" : 30,
        "analyzer"        : 31,
    }

    def __init__(self, sys_clk_freq=int(125e6),
        with_pcie     = True,  pcie_lanes=1,
        with_ethernet = False, ethernet_sfp=0,
        with_sata     = False, sata_gen="gen2",
        with_jtagbone = True
    ):
        # Platform ---------------------------------------------------------------------------------
        platform = Platform(build_multiboot=True)

        # SoCMini ----------------------------------------------------------------------------------

        SoCMini.__init__(self, platform, sys_clk_freq,
            ident         = f"LiteX SoC on LiteX-M2SDR",
            ident_version = True,
        )

        # Clocking ---------------------------------------------------------------------------------

        self.crg = CRG(platform, sys_clk_freq,
            with_ethernet = with_ethernet,
            with_sata     = with_sata,
        )

        # SI5351 Clock Generator -------------------------------------------------------------------

        self.si5351_i2c = I2CMaster(pads=platform.request("si5351_i2c"))
        self.si5351_pwm = PWM(platform.request("si5351_pwm"),
            default_enable = 1,
            default_width  = 1024,
            default_period = 2048,
        )

        # JTAGBone ---------------------------------------------------------------------------------

        if with_jtagbone:
            self.add_jtagbone()
            platform.add_period_constraint(self.jtagbone_phy.cd_jtag.clk, 1e9/20e6)
            platform.add_false_path_constraints(self.jtagbone_phy.cd_jtag.clk, self.crg.cd_sys.clk)

        # Leds -------------------------------------------------------------------------------------

        self.leds = LedChaser(
            pads         = platform.request_all("user_led"),
            sys_clk_freq = sys_clk_freq
        )

        # ICAP -------------------------------------------------------------------------------------

        self.icap = ICAP()
        self.icap.add_reload()
        self.icap.add_timing_constraints(platform, sys_clk_freq, self.crg.cd_sys.clk)

        # XADC -------------------------------------------------------------------------------------

        self.xadc = XADC()

        # DNA --------------------------------------------------------------------------------------

        self.dna = DNA()
        self.dna.add_timing_constraints(platform, sys_clk_freq, self.crg.cd_sys.clk)

        # SPI Flash --------------------------------------------------------------------------------
        self.flash_cs_n = GPIOOut(platform.request("flash_cs_n"))
        self.flash      = S7SPIFlash(platform.request("flash"), sys_clk_freq, 25e6)
        self.add_config("FLASH_IMAGE_SIZE", platform.image_size)

        # PCIe -------------------------------------------------------------------------------------

        if with_pcie:
            self.pcie_phy = S7PCIEPHY(platform, platform.request(f"pcie_x{pcie_lanes}"),
                data_width  = {1: 64, 4: 128}[pcie_lanes],
                bar0_size   = 0x20000,
                cd          = "sys",
            )
            if pcie_lanes == 1:
                platform.toolchain.pre_placement_commands.append("reset_property LOC [get_cells -hierarchical -filter {{NAME=~pcie_s7/*gtp_channel.gtpe2_channel_i}}]")
                platform.toolchain.pre_placement_commands.append("set_property LOC GTPE2_CHANNEL_X0Y4 [get_cells -hierarchical -filter {{NAME=~pcie_s7/*gtp_channel.gtpe2_channel_i}}]")
            self.pcie_phy.update_config({
                "Base_Class_Menu"          : "Wireless_controller",
                "Sub_Class_Interface_Menu" : "RF_controller",
                "Class_Code_Base"          : "0D",
                "Class_Code_Sub"           : "10",
                }
            )
            self.add_pcie(phy=self.pcie_phy, address_width=64, ndmas=1, data_width=64,
                with_dma_buffering    = True, dma_buffering_depth=8192,
                with_dma_loopback     = True,
                with_dma_synchronizer = True,
                with_msi              = True
            )
            self.comb += self.pcie_dma0.synchronizer.pps.eq(1)

            # Timing Constraints/False Paths -------------------------------------------------------
            for i in range(4):
                platform.toolchain.pre_placement_commands.append(f"set_clock_groups -group [get_clocks {{{{*s7pciephy_clkout{i}}}}}] -group [get_clocks        dna_clk] -asynchronous")
                platform.toolchain.pre_placement_commands.append(f"set_clock_groups -group [get_clocks {{{{*s7pciephy_clkout{i}}}}}] -group [get_clocks       jtag_clk] -asynchronous")
                platform.toolchain.pre_placement_commands.append(f"set_clock_groups -group [get_clocks {{{{*s7pciephy_clkout{i}}}}}] -group [get_clocks       icap_clk] -asynchronous")

        # Ethernet/SATA Shared Clocking ------------------------------------------------------------

        if with_ethernet or with_sata:
           # Ethernet QPLL Settings.
            qpll_eth_settings = QPLLSettings(
                refclksel  = 0b111,
                fbdiv      = 4,
                fbdiv_45   = 4,
                refclk_div = 1,
            )
            # SATA QPLL Settings.
            qpll_sata_settings = QPLLSettings(
                refclksel  = 0b111,
                fbdiv      = 5,
                fbdiv_45   = 4,
                refclk_div = 1,
            )

            # Shared QPLL.
            self.qpll = qpll = QPLL(
                gtgrefclk0    = self.crg.cd_eth_ref.clk,
                qpllsettings0 = qpll_eth_settings,
                qpllsettings1 = qpll_sata_settings,
            )
            platform.add_platform_command("set_property SEVERITY {{Warning}} [get_drc_checks REQP-49]")

        # Ethernet ---------------------------------------------------------------------------------

        if with_ethernet:
            self.ethphy = A7_1000BASEX(
                qpll_channel = qpll.channels[0],
                data_pads    = self.platform.request("sfp", ethernet_sfp),
                sys_clk_freq = sys_clk_freq,
                rx_polarity  = 1, # Inverted on M2SDR.
                tx_polarity  = 0, # Inverted on M2SDR and Acorn Baseboard Mini.
            )
            self.add_etherbone(phy=self.ethphy, ip_address="192.168.1.50")

        # SATA -------------------------------------------------------------------------------------

        if with_sata:
            # IOs
            _sata_io = [
                ("sata", 0,
                    # Inverted on M2SDR.
                    Subsignal("tx_p",  Pins("D7")),
                    Subsignal("tx_n",  Pins("C7")),
                    # Inverted on M2SDR.
                    Subsignal("rx_p",  Pins("D9")),
                    Subsignal("rx_n",  Pins("C9")),
                ),
            ]
            platform.add_extension(_sata_io)

            # PHY
            self.sata_phy = LiteSATAPHY(platform.device,
                refclk     = self.crg.cd_sata_ref.clk,
                pads       = platform.request("sata"),
                gen        = sata_gen,
                clk_freq   = sys_clk_freq,
                data_width = 16,
                qpll       = qpll.channels[1],
            )
            platform.add_platform_command("set_property SEVERITY {{WARNING}} [get_drc_checks REQP-49]")

            # Core
            self.add_sata(phy=self.sata_phy, mode="read+write")

        # Timestamp --------------------------------------------------------------------------------

        self.timestamp = Timestamp(clk_domain="rfic")

        # TX/RX Header Extracter/Inserter ----------------------------------------------------------

        self.header = TXRXHeader(data_width=64)
        self.comb += [
            self.header.rx.header.eq(0x5aa5_5aa5_5aa5_5aa5), # Unused for now, arbitrary.
            self.header.rx.timestamp.eq(self.timestamp.time),
        ]
        if with_pcie:
            # PCIe TX -> Header TX.
            self.comb += [
                self.header.tx.reset.eq(~self.pcie_dma0.reader.enable),
                self.pcie_dma0.source.connect(self.header.tx.sink),
            ]

            # Header RX -> PCIe RX.
            self.comb += [
                self.header.rx.reset.eq(~self.pcie_dma0.writer.enable),
                self.header.rx.source.connect(self.pcie_dma0.sink),
            ]

        # AD9361 RFIC ------------------------------------------------------------------------------

        self.ad9361 = AD9361RFIC(
            rfic_pads    = platform.request("ad9361_rfic"),
            spi_pads     = platform.request("ad9361_spi"),
            sys_clk_freq = sys_clk_freq,
        )
        self.ad9361.add_prbs()
        if with_pcie:
            self.comb += [
                # Header TX -> AD9361 TX.
                self.header.tx.source.connect(self.ad9361.sink),
                # AD9361 RX -> Header RX.
                self.ad9361.source.connect(self.header.rx.sink),
        ]
        #self.platform.add_period_constraint(self.ad9361.cd_rfic.clk, 1e9/245.76e6)
        self.platform.add_period_constraint(self.ad9361.cd_rfic.clk, 1e9/491.52e6)
        self.platform.add_false_path_constraints(self.crg.cd_sys.clk, self.ad9361.cd_rfic.clk)

        # Clk Measurements -------------------------------------------------------------------------

        self.clk_measurement = MultiClkMeasurement(clks={
            "clk0" : platform.request("si5351_clk0"),
            "clk1" : ClockSignal("rfic"),
            "clk2" : 0,
            "clk3" : 0,
        })

    def add_ad9361_spi_probe(self):
        analyzer_signals = [self.platform.lookup_request("ad9361_spi")]
        self.analyzer = LiteScopeAnalyzer(analyzer_signals,
            depth        = 4096,
            clock_domain = "sys",
            register     = True,
            csr_csv      = "analyzer.csv"
        )

    def add_ad96361_data_probe(self):
        analyzer_signals = [
            self.ad9361.phy.sink,   # TX.
            self.ad9361.phy.source, # RX.
            self.ad9361.prbs_rx.fields.synced,
        ]
        self.analyzer = LiteScopeAnalyzer(analyzer_signals,
            depth        = 4096,
            clock_domain = "rfic",
            register     = True,
            csr_csv      = "analyzer.csv"
        )

    def add_pcie_dma_probe(self):
        assert hasattr(self, "pcie_dma0")
        analyzer_signals = [
            self.pcie_dma0.sink,   # RX.
            self.pcie_dma0.source, # TX.
            self.pcie_dma0.synchronizer.synced,
        ]
        self.analyzer = LiteScopeAnalyzer(analyzer_signals,
            depth        = 1024,
            clock_domain = "sys",
            register     = True,
            csr_csv      = "analyzer.csv"
        )

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="LiteX SoC on LiteX-M2SDR.")
    # Build/Load/Utilities.
    parser.add_argument("--build",           action="store_true", help="Build bitstream.")
    parser.add_argument("--load",            action="store_true", help="Load bitstream.")
    parser.add_argument("--flash",           action="store_true", help="Flash bitstream.")
    parser.add_argument("--flash-multiboot", action="store_true", help="Flash multiboot bitstreams.")
    parser.add_argument("--rescan",          action="store_true", help="Execute PCIe Rescan while Loading/Flashing.")
    parser.add_argument("--driver",          action="store_true", help="Generate PCIe driver from LitePCIe (override local version).")

    # Communication interfaces/features.
    comopts = parser.add_mutually_exclusive_group()
    comopts.add_argument("--with-pcie",      action="store_true", help="Enable PCIe Communication.")
    comopts.add_argument("--with-ethernet",  action="store_true", help="Enable Ethernet Communication.")
    comopts.add_argument("--with-sata",      action="store_true", help="Enable SATA Storage.")
    parser.add_argument("--pcie-lanes",      default=4, type=int, help="PCIe Lanes.",   choices=[1, 4])
    parser.add_argument("--ethernet-sfp",    default=0, type=int, help="Ethernet SFP.", choices=[0, 1])

    # Litescope Probes.
    probeopts = parser.add_mutually_exclusive_group()
    probeopts.add_argument("--with-ad9361-spi-probe",  action="store_true", help="Enable AD9361 SPI Probe.")
    probeopts.add_argument("--with-ad9361-data-probe", action="store_true", help="Enable AD9361 Data Probe.")
    probeopts.add_argument("--with-pcie-dma-probe",    action="store_true", help="Enable PCIe DMA Probe.")

    args = parser.parse_args()

    # Build SoC.
    soc = BaseSoC(
        with_pcie     = args.with_pcie,
        pcie_lanes    = args.pcie_lanes,
        with_ethernet = args.with_ethernet,
        ethernet_sfp  = args.ethernet_sfp,
        with_sata     = args.with_sata,
    )
    if args.with_ad9361_spi_probe:
        soc.add_ad9361_spi_probe()
    if args.with_ad9361_data_probe:
        soc.add_ad96361_data_probe()
    if args.with_pcie_dma_probe:
        soc.add_pcie_dma_probe()

    builder = Builder(soc, csr_csv="csr.csv")
    builder.build(run=args.build)

    # Generate LitePCIe Driver.
    generate_litepcie_software(soc, "software", use_litepcie_software=args.driver)

    # Remove PCIe Driver/Device.
    if (args.load or args.flash) and args.rescan:
        device_id = get_pcie_device_id(vendor="10ee", device="7021") # FIXME: Handle X4  case.
        if device_id:
            remove_pcie_device(device_id, driver="litepcie")

    # Load Bistream.
    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(os.path.join(builder.gateware_dir, soc.build_name + ".bit"))

    # Flash Bitstream.
    if args.flash:
        prog = soc.platform.create_programmer()
        prog.flash(0, os.path.join(builder.gateware_dir, soc.build_name + ".bin"))

    # Flash Multiboot Bitstreams.
    if args.flash_multiboot:
        prog = soc.platform.create_programmer()
        prog.flash(            0x0000_0000,  builder.get_bitstream_filename(mode="flash").replace(".bin", "_fallback.bin"),   verify=True)
        prog.flash(soc.platform.image_size, builder.get_bitstream_filename(mode="flash").replace(".bin", "_operational.bin"), verify=True)

    # Rescan PCIe Bus.
    if args.rescan:
        time.sleep(2)
        rescan_pcie_bus()

if __name__ == "__main__":
    main()
