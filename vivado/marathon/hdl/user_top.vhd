----------------------------------------------------------------------------------
-- user_top: the single module reference the block design instantiates for all
-- user logic. Everything below this line is plain RTL hierarchy -- adding a
-- submodule no longer means adding a BD cell, wiring its clock/reset by hand
-- and re-exporting bd_CoraZ7_Eth.tcl.
--
-- BOUNDARY RULES (learned the hard way, see the axi_tdm_filter aclk rename):
--   * Ports must stay FLAT. Vivado infers AXI interfaces on a module reference
--     from port NAMES only -- a VHDL record port cannot be inferred and shows
--     up in the BD as a loose port the interconnect cannot connect to.
--     X_INTERFACE_* attributes do not help; Vivado ignores them for VHDL
--     module references. Records are fine BELOW this boundary.
--   * The clock is plain `aclk`, not `<prefix>_aclk`. A prefixed clock
--     associates only with the interface of the same prefix, which leaves any
--     other interface with no clock, a default 100MHz FREQ_HZ, and a BD that
--     fails validation against the 50MHz masters.
--   * AXI-Lite slaves are named s<nn>_axi_*. s00_axi is deliberately left free
--     for the future consolidated bus (one slave + address decode in here),
--     which is what stops the port count growing with the module count.
--
-- Currently hosts:
--   fpga_top             LED PWM blink, bring-up sanity check, no bus
--   axi_processing_ch1   ch1 filter chain, AXI-Lite on s01_axi @ 0x40001000
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

entity user_top is
    generic (
        -- Forwarded to axi_processing_ch1. Kept here rather than left at the
        -- submodule default so the filter constant is visible at the BD cell.
        SHIFT                 : integer := 4;
        C_S01_AXI_DATA_WIDTH  : integer := 32;
        C_S01_AXI_ADDR_WIDTH  : integer := 4
    );
    port (
        -- Bare aclk/aresetn: associates with every inferred interface.
        aclk            : in  std_logic;
        aresetn         : in  std_logic;

        -- AXI4-Lite slave: axi_processing_ch1 control/status
        s01_axi_awaddr  : in  std_logic_vector(C_S01_AXI_ADDR_WIDTH-1 downto 0);
        s01_axi_awprot  : in  std_logic_vector(2 downto 0);
        s01_axi_awvalid : in  std_logic;
        s01_axi_awready : out std_logic;
        s01_axi_wdata   : in  std_logic_vector(C_S01_AXI_DATA_WIDTH-1 downto 0);
        s01_axi_wstrb   : in  std_logic_vector((C_S01_AXI_DATA_WIDTH/8)-1 downto 0);
        s01_axi_wvalid  : in  std_logic;
        s01_axi_wready  : out std_logic;
        s01_axi_bresp   : out std_logic_vector(1 downto 0);
        s01_axi_bvalid  : out std_logic;
        s01_axi_bready  : in  std_logic;
        s01_axi_araddr  : in  std_logic_vector(C_S01_AXI_ADDR_WIDTH-1 downto 0);
        s01_axi_arprot  : in  std_logic_vector(2 downto 0);
        s01_axi_arvalid : in  std_logic;
        s01_axi_arready : out std_logic;
        s01_axi_rdata   : out std_logic_vector(C_S01_AXI_DATA_WIDTH-1 downto 0);
        s01_axi_rresp   : out std_logic_vector(1 downto 0);
        s01_axi_rvalid  : out std_logic;
        s01_axi_rready  : in  std_logic;

        -- Board I/O
        btn_pl          : in  std_logic;
        led_pl_b        : out std_logic;
        led_pl_g        : out std_logic;
        led_pl_r        : out std_logic
    );
end user_top;

architecture rtl of user_top is

    component fpga_top is
        port (
            clk      : in  std_logic;
            btn_pl   : in  std_logic;
            led_pl_b : out std_logic;
            led_pl_g : out std_logic;
            led_pl_r : out std_logic
        );
    end component;

    component axi_processing_ch1 is
        generic (
            SHIFT                 : integer := 4;
            C_S00_AXI_DATA_WIDTH  : integer := 32;
            C_S00_AXI_ADDR_WIDTH  : integer := 4
        );
        port (
            s00_axi_aclk    : in  std_logic;
            s00_axi_aresetn : in  std_logic;
            s00_axi_awaddr  : in  std_logic_vector(C_S00_AXI_ADDR_WIDTH-1 downto 0);
            s00_axi_awprot  : in  std_logic_vector(2 downto 0);
            s00_axi_awvalid : in  std_logic;
            s00_axi_awready : out std_logic;
            s00_axi_wdata   : in  std_logic_vector(C_S00_AXI_DATA_WIDTH-1 downto 0);
            s00_axi_wstrb   : in  std_logic_vector((C_S00_AXI_DATA_WIDTH/8)-1 downto 0);
            s00_axi_wvalid  : in  std_logic;
            s00_axi_wready  : out std_logic;
            s00_axi_bresp   : out std_logic_vector(1 downto 0);
            s00_axi_bvalid  : out std_logic;
            s00_axi_bready  : in  std_logic;
            s00_axi_araddr  : in  std_logic_vector(C_S00_AXI_ADDR_WIDTH-1 downto 0);
            s00_axi_arprot  : in  std_logic_vector(2 downto 0);
            s00_axi_arvalid : in  std_logic;
            s00_axi_arready : out std_logic;
            s00_axi_rdata   : out std_logic_vector(C_S00_AXI_DATA_WIDTH-1 downto 0);
            s00_axi_rresp   : out std_logic_vector(1 downto 0);
            s00_axi_rvalid  : out std_logic;
            s00_axi_rready  : in  std_logic
        );
    end component;

begin

    fpga_top_led : fpga_top
        port map (
            clk      => aclk,
            btn_pl   => btn_pl,
            led_pl_b => led_pl_b,
            led_pl_g => led_pl_g,
            led_pl_r => led_pl_r
        );

    -- s01_axi at this boundary maps straight onto the submodule's s00_axi.
    -- The submodule keeps its own naming; only the BD-facing name is renumbered.
    ch1 : axi_processing_ch1
        generic map (
            SHIFT                 => SHIFT,
            C_S00_AXI_DATA_WIDTH  => C_S01_AXI_DATA_WIDTH,
            C_S00_AXI_ADDR_WIDTH  => C_S01_AXI_ADDR_WIDTH
        )
        port map (
            s00_axi_aclk    => aclk,
            s00_axi_aresetn => aresetn,
            s00_axi_awaddr  => s01_axi_awaddr,
            s00_axi_awprot  => s01_axi_awprot,
            s00_axi_awvalid => s01_axi_awvalid,
            s00_axi_awready => s01_axi_awready,
            s00_axi_wdata   => s01_axi_wdata,
            s00_axi_wstrb   => s01_axi_wstrb,
            s00_axi_wvalid  => s01_axi_wvalid,
            s00_axi_wready  => s01_axi_wready,
            s00_axi_bresp   => s01_axi_bresp,
            s00_axi_bvalid  => s01_axi_bvalid,
            s00_axi_bready  => s01_axi_bready,
            s00_axi_araddr  => s01_axi_araddr,
            s00_axi_arprot  => s01_axi_arprot,
            s00_axi_arvalid => s01_axi_arvalid,
            s00_axi_arready => s01_axi_arready,
            s00_axi_rdata   => s01_axi_rdata,
            s00_axi_rresp   => s01_axi_rresp,
            s00_axi_rvalid  => s01_axi_rvalid,
            s00_axi_rready  => s01_axi_rready
        );

end rtl;
