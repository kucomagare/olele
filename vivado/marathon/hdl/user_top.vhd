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
-- The entity is flat because Vivado's module-reference resolver cannot see an
-- entity that has a record port -- verified, not assumed: an otherwise
-- identical entity resolves with flat ports and fails with "Unable to resolve
-- module-source" with record ports. It is packed into axil_pkg records on the
-- first lines of the architecture; everything below works on records.
--
-- Hosts:
--   fpga_top             LED PWM blink, bring-up sanity check, no bus
--   axi_tdm_filter       TDM streaming filter, s00_axi @ 0x40000000,
--                        streams to/from the DMA via s_axis/m_axis
--   axi_processing_ch1   ch1 filter chain, s01_axi @ 0x40001000
--   axi_processing_ch2   ch2 filter chain, s02_axi @ 0x40002000
--
-- Three windows because each module still owns a my_axi. The central register
-- file collapses both into one slave on one window; until then there is
-- nothing to route and no reason for routing logic.
--
-- ADDRESS MAP: one segment, 8K at 0x40001000, addr[12] selects the channel.
-- Sized and placed so both channels keep the addresses they had as separate BD
-- cells -- ch1 0x40001000, ch2 0x40002000 -- which is why this restructuring
-- needs no firmware change at all. Step 4 widens the same window to 16K at
-- 0x40000000 to absorb axi_tdm_filter without moving anything either.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

library work;
use work.axil_pkg.all;

entity user_top is
    generic (
        -- Per-channel filter constant, forwarded to the submodules. Kept here
        -- rather than left at the submodule default so both are visible and
        -- independently settable at the BD cell.
        -- Depth of axi_tdm_filter's per-channel state RAM.
        MAX_CHANNELS      : integer := 64;
        CH1_SHIFT         : integer := 4;
        CH2_SHIFT         : integer := 4;
        C_AXI_DATA_WIDTH  : integer := 32;
        C_AXI_ADDR_WIDTH  : integer := 4
    );
    port (
        -- Bare aclk/aresetn: associates with every inferred interface.
        aclk            : in  std_logic;
        aresetn         : in  std_logic;

        -- AXI4-Lite slave: axi_tdm_filter control/status
        s00_axi_awaddr  : in  std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
        s00_axi_awprot  : in  std_logic_vector(2 downto 0);
        s00_axi_awvalid : in  std_logic;
        s00_axi_awready : out std_logic;
        s00_axi_wdata   : in  std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
        s00_axi_wstrb   : in  std_logic_vector((C_AXI_DATA_WIDTH/8)-1 downto 0);
        s00_axi_wvalid  : in  std_logic;
        s00_axi_wready  : out std_logic;
        s00_axi_bresp   : out std_logic_vector(1 downto 0);
        s00_axi_bvalid  : out std_logic;
        s00_axi_bready  : in  std_logic;
        s00_axi_araddr  : in  std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
        s00_axi_arprot  : in  std_logic_vector(2 downto 0);
        s00_axi_arvalid : in  std_logic;
        s00_axi_arready : out std_logic;
        s00_axi_rdata   : out std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
        s00_axi_rresp   : out std_logic_vector(1 downto 0);
        s00_axi_rvalid  : out std_logic;
        s00_axi_rready  : in  std_logic;

        -- AXI4-Stream to/from the DMA. Flat for the same reason the AXI-Lite
        -- windows are: name-based interface inference at the BD boundary.
        s_axis_tdata    : in  std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
        s_axis_tvalid   : in  std_logic;
        s_axis_tready   : out std_logic;
        s_axis_tlast    : in  std_logic;

        m_axis_tdata    : out std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
        m_axis_tvalid   : out std_logic;
        m_axis_tready   : in  std_logic;
        m_axis_tlast    : out std_logic;

        -- AXI4-Lite slave: axi_processing_ch1 control/status
        s01_axi_awaddr  : in  std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
        s01_axi_awprot  : in  std_logic_vector(2 downto 0);
        s01_axi_awvalid : in  std_logic;
        s01_axi_awready : out std_logic;
        s01_axi_wdata   : in  std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
        s01_axi_wstrb   : in  std_logic_vector((C_AXI_DATA_WIDTH/8)-1 downto 0);
        s01_axi_wvalid  : in  std_logic;
        s01_axi_wready  : out std_logic;
        s01_axi_bresp   : out std_logic_vector(1 downto 0);
        s01_axi_bvalid  : out std_logic;
        s01_axi_bready  : in  std_logic;
        s01_axi_araddr  : in  std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
        s01_axi_arprot  : in  std_logic_vector(2 downto 0);
        s01_axi_arvalid : in  std_logic;
        s01_axi_arready : out std_logic;
        s01_axi_rdata   : out std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
        s01_axi_rresp   : out std_logic_vector(1 downto 0);
        s01_axi_rvalid  : out std_logic;
        s01_axi_rready  : in  std_logic;

        -- AXI4-Lite slave: axi_processing_ch2 control/status
        s02_axi_awaddr  : in  std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
        s02_axi_awprot  : in  std_logic_vector(2 downto 0);
        s02_axi_awvalid : in  std_logic;
        s02_axi_awready : out std_logic;
        s02_axi_wdata   : in  std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
        s02_axi_wstrb   : in  std_logic_vector((C_AXI_DATA_WIDTH/8)-1 downto 0);
        s02_axi_wvalid  : in  std_logic;
        s02_axi_wready  : out std_logic;
        s02_axi_bresp   : out std_logic_vector(1 downto 0);
        s02_axi_bvalid  : out std_logic;
        s02_axi_bready  : in  std_logic;
        s02_axi_araddr  : in  std_logic_vector(C_AXI_ADDR_WIDTH-1 downto 0);
        s02_axi_arprot  : in  std_logic_vector(2 downto 0);
        s02_axi_arvalid : in  std_logic;
        s02_axi_arready : out std_logic;
        s02_axi_rdata   : out std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
        s02_axi_rresp   : out std_logic_vector(1 downto 0);
        s02_axi_rvalid  : out std_logic;
        s02_axi_rready  : in  std_logic;

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

    component axi_tdm_filter is
        generic (
            MAX_CHANNELS          : integer := 64;
            C_S00_AXI_DATA_WIDTH  : integer := 32;
            C_S00_AXI_ADDR_WIDTH  : integer := 4
        );
        port (
            aclk        : in  std_logic;
            aresetn     : in  std_logic;
            s_m2s       : in  t_axil_m2s;
            s_s2m       : out t_axil_s2m;
            s_axis_m2s  : in  t_axis_m2s;
            s_axis_s2m  : out t_axis_s2m;
            m_axis_m2s  : out t_axis_m2s;
            m_axis_s2m  : in  t_axis_s2m
        );
    end component;

    component axi_processing_ch1 is
        generic (
            SHIFT                 : integer := 4;
            C_S00_AXI_DATA_WIDTH  : integer := 32;
            C_S00_AXI_ADDR_WIDTH  : integer := 4
        );
        port (
            aclk    : in  std_logic;
            aresetn : in  std_logic;
            s_m2s   : in  t_axil_m2s;
            s_s2m   : out t_axil_s2m
        );
    end component;

    component axi_processing_ch2 is
        generic (
            SHIFT                 : integer := 4;
            C_S00_AXI_DATA_WIDTH  : integer := 32;
            C_S00_AXI_ADDR_WIDTH  : integer := 4
        );
        port (
            aclk    : in  std_logic;
            aresetn : in  std_logic;
            s_m2s   : in  t_axil_m2s;
            s_s2m   : out t_axil_s2m
        );
    end component;

    signal tdm_m2s, ch1_m2s, ch2_m2s : t_axil_m2s;
    signal tdm_s2m, ch1_s2m, ch2_s2m : t_axil_s2m;

    signal tdm_s_axis_m2s, tdm_m_axis_m2s : t_axis_m2s;
    signal tdm_s_axis_s2m, tdm_m_axis_s2m : t_axis_s2m;

begin

    ------------------------------------------------------------------------
    -- Flat -> record, once per window. The only place in the design the bus
    -- appears as loose signals: Vivado infers BD interfaces from port names,
    -- so this entity cannot take a record (verified -- an otherwise identical
    -- entity fails to resolve as a module reference with record ports).
    ------------------------------------------------------------------------
    tdm_m2s.awaddr  <= std_logic_vector(resize(unsigned(s00_axi_awaddr), AXIL_ADDR_W));
    tdm_m2s.awprot  <= s00_axi_awprot;
    tdm_m2s.awvalid <= s00_axi_awvalid;
    tdm_m2s.wdata   <= s00_axi_wdata;
    tdm_m2s.wstrb   <= s00_axi_wstrb;
    tdm_m2s.wvalid  <= s00_axi_wvalid;
    tdm_m2s.bready  <= s00_axi_bready;
    tdm_m2s.araddr  <= std_logic_vector(resize(unsigned(s00_axi_araddr), AXIL_ADDR_W));
    tdm_m2s.arprot  <= s00_axi_arprot;
    tdm_m2s.arvalid <= s00_axi_arvalid;
    tdm_m2s.rready  <= s00_axi_rready;

    s00_axi_awready <= tdm_s2m.awready;
    s00_axi_wready  <= tdm_s2m.wready;
    s00_axi_bresp   <= tdm_s2m.bresp;
    s00_axi_bvalid  <= tdm_s2m.bvalid;
    s00_axi_arready <= tdm_s2m.arready;
    s00_axi_rdata   <= tdm_s2m.rdata;
    s00_axi_rresp   <= tdm_s2m.rresp;
    s00_axi_rvalid  <= tdm_s2m.rvalid;

    tdm_s_axis_m2s.tdata  <= s_axis_tdata;
    tdm_s_axis_m2s.tvalid <= s_axis_tvalid;
    tdm_s_axis_m2s.tlast  <= s_axis_tlast;
    s_axis_tready         <= tdm_s_axis_s2m.tready;

    m_axis_tdata          <= tdm_m_axis_m2s.tdata;
    m_axis_tvalid         <= tdm_m_axis_m2s.tvalid;
    m_axis_tlast          <= tdm_m_axis_m2s.tlast;
    tdm_m_axis_s2m.tready <= m_axis_tready;

    ch1_m2s.awaddr  <= std_logic_vector(resize(unsigned(s01_axi_awaddr), AXIL_ADDR_W));
    ch1_m2s.awprot  <= s01_axi_awprot;
    ch1_m2s.awvalid <= s01_axi_awvalid;
    ch1_m2s.wdata   <= s01_axi_wdata;
    ch1_m2s.wstrb   <= s01_axi_wstrb;
    ch1_m2s.wvalid  <= s01_axi_wvalid;
    ch1_m2s.bready  <= s01_axi_bready;
    ch1_m2s.araddr  <= std_logic_vector(resize(unsigned(s01_axi_araddr), AXIL_ADDR_W));
    ch1_m2s.arprot  <= s01_axi_arprot;
    ch1_m2s.arvalid <= s01_axi_arvalid;
    ch1_m2s.rready  <= s01_axi_rready;

    s01_axi_awready <= ch1_s2m.awready;
    s01_axi_wready  <= ch1_s2m.wready;
    s01_axi_bresp   <= ch1_s2m.bresp;
    s01_axi_bvalid  <= ch1_s2m.bvalid;
    s01_axi_arready <= ch1_s2m.arready;
    s01_axi_rdata   <= ch1_s2m.rdata;
    s01_axi_rresp   <= ch1_s2m.rresp;
    s01_axi_rvalid  <= ch1_s2m.rvalid;

    ch2_m2s.awaddr  <= std_logic_vector(resize(unsigned(s02_axi_awaddr), AXIL_ADDR_W));
    ch2_m2s.awprot  <= s02_axi_awprot;
    ch2_m2s.awvalid <= s02_axi_awvalid;
    ch2_m2s.wdata   <= s02_axi_wdata;
    ch2_m2s.wstrb   <= s02_axi_wstrb;
    ch2_m2s.wvalid  <= s02_axi_wvalid;
    ch2_m2s.bready  <= s02_axi_bready;
    ch2_m2s.araddr  <= std_logic_vector(resize(unsigned(s02_axi_araddr), AXIL_ADDR_W));
    ch2_m2s.arprot  <= s02_axi_arprot;
    ch2_m2s.arvalid <= s02_axi_arvalid;
    ch2_m2s.rready  <= s02_axi_rready;

    s02_axi_awready <= ch2_s2m.awready;
    s02_axi_wready  <= ch2_s2m.wready;
    s02_axi_bresp   <= ch2_s2m.bresp;
    s02_axi_bvalid  <= ch2_s2m.bvalid;
    s02_axi_arready <= ch2_s2m.arready;
    s02_axi_rdata   <= ch2_s2m.rdata;
    s02_axi_rresp   <= ch2_s2m.rresp;
    s02_axi_rvalid  <= ch2_s2m.rvalid;

    ------------------------------------------------------------------------

    fpga_top_led : fpga_top
        port map (
            clk      => aclk,
            btn_pl   => btn_pl,
            led_pl_b => led_pl_b,
            led_pl_g => led_pl_g,
            led_pl_r => led_pl_r
        );

    tdm : axi_tdm_filter
        generic map (
            MAX_CHANNELS          => MAX_CHANNELS,
            C_S00_AXI_DATA_WIDTH  => AXIL_DATA_W,
            C_S00_AXI_ADDR_WIDTH  => C_AXI_ADDR_WIDTH
        )
        port map (
            aclk       => aclk,
            aresetn    => aresetn,
            s_m2s      => tdm_m2s,
            s_s2m      => tdm_s2m,
            s_axis_m2s => tdm_s_axis_m2s,
            s_axis_s2m => tdm_s_axis_s2m,
            m_axis_m2s => tdm_m_axis_m2s,
            m_axis_s2m => tdm_m_axis_s2m
        );

    ch1 : axi_processing_ch1
        generic map (
            SHIFT                 => CH1_SHIFT,
            C_S00_AXI_DATA_WIDTH  => AXIL_DATA_W,
            C_S00_AXI_ADDR_WIDTH  => C_AXI_ADDR_WIDTH
        )
        port map (
            aclk    => aclk,
            aresetn => aresetn,
            s_m2s   => ch1_m2s,
            s_s2m   => ch1_s2m
        );

    ch2 : axi_processing_ch2
        generic map (
            SHIFT                 => CH2_SHIFT,
            C_S00_AXI_DATA_WIDTH  => AXIL_DATA_W,
            C_S00_AXI_ADDR_WIDTH  => C_AXI_ADDR_WIDTH
        )
        port map (
            aclk    => aclk,
            aresetn => aresetn,
            s_m2s   => ch2_m2s,
            s_s2m   => ch2_s2m
        );

end rtl;
