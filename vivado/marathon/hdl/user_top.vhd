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
--   * There is exactly ONE AXI-Lite slave, s00_axi. Every control/status
--     register in the design sits behind it, so the port count no longer grows
--     with the module count. A new register means a line in csr_top, never a
--     new port here.
--
-- The entity is flat because Vivado's module-reference resolver cannot see an
-- entity that has a record port -- verified, not assumed: an otherwise
-- identical entity resolves with flat ports and fails with "Unable to resolve
-- module-source" with record ports. It is packed into axil_pkg records on the
-- first lines of the architecture; everything below works on records.
--
-- Hosts:
--   fpga_top             LED PWM blink, bring-up sanity check, no bus
--   csr_top              the only bus slave: all registers, the address decode
--                        and the read mux. Submodules see plain wires from it,
--                        never the bus.
--   axi_tdm_filter       TDM streaming filter, streams to/from the DMA via
--                        s_axis/m_axis; its config comes from csr_top
--   axi_processing_ch1   ch1 per-sample filter: x/x_wr in from csr_top, y back
--   axi_processing_ch2   ch2 per-sample filter: same as ch1
--
-- ADDRESS MAP: one segment, 16K at 0x40000000, offsets decoded in csr_top.
--   0x0000  axi_tdm_filter   +0x0 N_CHANNELS  +0x4 SHIFT  +0x8 CONTROL
--                            +0xC STATUS (read-only)
--   0x1000  axi_processing_ch1   +0x0 X_IN (write pulse)  +0xC Y_OUT (read-only)
--   0x2000  axi_processing_ch2   same layout as ch1
--   0x3000  unmapped: reads 0, writes ignored, the bus still answers OKAY
-- These are the addresses the modules already had as separate slaves, which is
-- why the consolidation needs no firmware change.
--
-- C_AXI_ADDR_WIDTH must be 14. csr_top decodes address bits [13:12] to pick the
-- module; a narrower port drops those bits and every module aliases onto the
-- first one's registers. The BD does not override this generic, so this default
-- is the value in use.
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
    C_AXI_ADDR_WIDTH  : integer := 14
    );
  port (
    -- Bare aclk/aresetn: associates with every inferred interface.
    aclk            : in  std_logic;
    aresetn         : in  std_logic;

    -- AXI4-Lite slave: the whole register file (see ADDRESS MAP above)
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
    -- window is: name-based interface inference at the BD boundary.
    s_axis_tdata    : in  std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
    s_axis_tvalid   : in  std_logic;
    s_axis_tready   : out std_logic;
    s_axis_tlast    : in  std_logic;

    m_axis_tdata    : out std_logic_vector(C_AXI_DATA_WIDTH-1 downto 0);
    m_axis_tvalid   : out std_logic;
    m_axis_tready   : in  std_logic;
    m_axis_tlast    : out std_logic;

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

  component csr_top is
    generic (
      C_S_AXI_ADDR_WIDTH : integer := 14
      );
    port (
      aclk         : in  std_logic;
      aresetn      : in  std_logic;
      s_m2s        : in  t_axil_m2s;
      s_s2m        : out t_axil_s2m;

      tdm_cfg_reg0 : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      tdm_cfg_reg1 : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      tdm_cfg_reg2 : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      tdm_status   : in  std_logic_vector(AXIL_DATA_W-1 downto 0);

      ch1_x        : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      ch1_x_wr     : out std_logic;
      ch1_y        : in  std_logic_vector(AXIL_DATA_W-1 downto 0);
      ch2_x        : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      ch2_x_wr     : out std_logic;
      ch2_y        : in  std_logic_vector(AXIL_DATA_W-1 downto 0)
      );
  end component;

  component axi_tdm_filter is
    generic (
      MAX_CHANNELS : integer := 64
      );
    port (
      aclk       : in  std_logic;
      aresetn    : in  std_logic;
      s_axis_m2s : in  t_axis_m2s;
      s_axis_s2m : out t_axis_s2m;
      m_axis_m2s : out t_axis_m2s;
      m_axis_s2m : in  t_axis_s2m;
      cfg_reg0   : in  std_logic_vector(31 downto 0);
      cfg_reg1   : in  std_logic_vector(31 downto 0);
      cfg_reg2   : in  std_logic_vector(31 downto 0);
      status     : out std_logic_vector(31 downto 0)
      );
  end component;

  component axi_processing_ch1 is
    generic (
      SHIFT : integer := 4
      );
    port (
      aclk             : in  std_logic;
      aresetn          : in  std_logic;
      axi_slv_reg_wren : in  std_logic;
      axi_slv_reg0     : in  std_logic_vector(31 downto 0);
      filtered_result  : out std_logic_vector(31 downto 0)
      );
  end component;

  component axi_processing_ch2 is
    generic (
      SHIFT : integer := 4
      );
    port (
      aclk             : in  std_logic;
      aresetn          : in  std_logic;
      axi_slv_reg_wren : in  std_logic;
      axi_slv_reg0     : in  std_logic_vector(31 downto 0);
      filtered_result  : out std_logic_vector(31 downto 0)
      );
  end component;

  -- The one AXI-Lite bus, packed into records.
  signal csr_m2s : t_axil_m2s;
  signal csr_s2m : t_axil_s2m;

  -- csr_top <-> axi_tdm_filter
  signal tdm_cfg_reg0, tdm_cfg_reg1, tdm_cfg_reg2 : std_logic_vector(AXIL_DATA_W-1 downto 0);
  signal tdm_status                               : std_logic_vector(AXIL_DATA_W-1 downto 0);

  -- csr_top <-> per-channel filters: sample in + write pulse, result back
  signal ch1_x, ch1_y, ch2_x, ch2_y : std_logic_vector(AXIL_DATA_W-1 downto 0);
  signal ch1_x_wr, ch2_x_wr         : std_logic;

  signal tdm_s_axis_m2s, tdm_m_axis_m2s : t_axis_m2s;
  signal tdm_s_axis_s2m, tdm_m_axis_s2m : t_axis_s2m;

begin

  ------------------------------------------------------------------------
  -- Flat -> record, once. The only place in the design the bus appears as
  -- loose signals: Vivado infers BD interfaces from port names, so this
  -- entity cannot take a record (verified -- an otherwise identical entity
  -- fails to resolve as a module reference with record ports).
  ------------------------------------------------------------------------
  csr_m2s.awaddr  <= std_logic_vector(resize(unsigned(s00_axi_awaddr), AXIL_ADDR_W));
  csr_m2s.awprot  <= s00_axi_awprot;
  csr_m2s.awvalid <= s00_axi_awvalid;
  csr_m2s.wdata   <= s00_axi_wdata;
  csr_m2s.wstrb   <= s00_axi_wstrb;
  csr_m2s.wvalid  <= s00_axi_wvalid;
  csr_m2s.bready  <= s00_axi_bready;
  csr_m2s.araddr  <= std_logic_vector(resize(unsigned(s00_axi_araddr), AXIL_ADDR_W));
  csr_m2s.arprot  <= s00_axi_arprot;
  csr_m2s.arvalid <= s00_axi_arvalid;
  csr_m2s.rready  <= s00_axi_rready;

  s00_axi_awready <= csr_s2m.awready;
  s00_axi_wready  <= csr_s2m.wready;
  s00_axi_bresp   <= csr_s2m.bresp;
  s00_axi_bvalid  <= csr_s2m.bvalid;
  s00_axi_arready <= csr_s2m.arready;
  s00_axi_rdata   <= csr_s2m.rdata;
  s00_axi_rresp   <= csr_s2m.rresp;
  s00_axi_rvalid  <= csr_s2m.rvalid;

  tdm_s_axis_m2s.tdata  <= s_axis_tdata;
  tdm_s_axis_m2s.tvalid <= s_axis_tvalid;
  tdm_s_axis_m2s.tlast  <= s_axis_tlast;
  s_axis_tready         <= tdm_s_axis_s2m.tready;

  m_axis_tdata          <= tdm_m_axis_m2s.tdata;
  m_axis_tvalid         <= tdm_m_axis_m2s.tvalid;
  m_axis_tlast          <= tdm_m_axis_m2s.tlast;
  tdm_m_axis_s2m.tready <= m_axis_tready;

  ------------------------------------------------------------------------

  fpga_top_led : fpga_top
    port map (
      clk      => aclk,
      btn_pl   => btn_pl,
      led_pl_b => led_pl_b,
      led_pl_g => led_pl_g,
      led_pl_r => led_pl_r
      );

  csr : csr_top
    generic map (
      C_S_AXI_ADDR_WIDTH => C_AXI_ADDR_WIDTH
      )
    port map (
      aclk         => aclk,
      aresetn      => aresetn,
      s_m2s        => csr_m2s,
      s_s2m        => csr_s2m,
      tdm_cfg_reg0 => tdm_cfg_reg0,
      tdm_cfg_reg1 => tdm_cfg_reg1,
      tdm_cfg_reg2 => tdm_cfg_reg2,
      tdm_status   => tdm_status,
      ch1_x        => ch1_x,
      ch1_x_wr     => ch1_x_wr,
      ch1_y        => ch1_y,
      ch2_x        => ch2_x,
      ch2_x_wr     => ch2_x_wr,
      ch2_y        => ch2_y
      );

  tdm : axi_tdm_filter
    generic map (
      MAX_CHANNELS => MAX_CHANNELS
      )
    port map (
      aclk       => aclk,
      aresetn    => aresetn,
      s_axis_m2s => tdm_s_axis_m2s,
      s_axis_s2m => tdm_s_axis_s2m,
      m_axis_m2s => tdm_m_axis_m2s,
      m_axis_s2m => tdm_m_axis_s2m,
      cfg_reg0   => tdm_cfg_reg0,
      cfg_reg1   => tdm_cfg_reg1,
      cfg_reg2   => tdm_cfg_reg2,
      status     => tdm_status
      );

  ch1 : axi_processing_ch1
    generic map (
      SHIFT => CH1_SHIFT
      )
    port map (
      aclk             => aclk,
      aresetn          => aresetn,
      axi_slv_reg_wren => ch1_x_wr,
      axi_slv_reg0     => ch1_x,
      filtered_result  => ch1_y
      );

  ch2 : axi_processing_ch2
    generic map (
      SHIFT => CH2_SHIFT
      )
    port map (
      aclk             => aclk,
      aresetn          => aresetn,
      axi_slv_reg_wren => ch2_x_wr,
      axi_slv_reg0     => ch2_x,
      filtered_result  => ch2_y
      );

end rtl;
