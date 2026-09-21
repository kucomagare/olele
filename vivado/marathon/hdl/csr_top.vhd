----------------------------------------------------------------------------------
-- csr_top: the design's control/status register file, and the only AXI-Lite
-- slave. Three parts:
--
--   my_axi     protocol engine: AXI-Lite handshakes -> wr_en/wr_addr/wr_data/
--              wr_strb and rd_en/rd_addr/rd_data (no registers of its own)
--   csr_reg    one instance per writable register (storage + byte-lane writes)
--   decode     the write strobes and the read mux, below
--
-- Submodules never see the bus: each register value leaves as a plain port, so
-- adding a register means one address constant, one strobe, one csr_reg, and one
-- line in the read mux -- my_axi is not touched.
--
-- REGISTER MAP (offsets inside the 16K window at 0x40000000):
--   0x0000  TDM_N_CHANNELS  RW   -> tdm_cfg_reg0 (reads back the raw value)
--   0x0004  TDM_SHIFT       RW   -> tdm_cfg_reg1
--   0x0008  TDM_CONTROL     RW   -> tdm_cfg_reg2
--   0x000C  TDM_STATUS      RO   <- tdm_status (live)
--   0x1000  CH1_X_IN        RW   -> ch1_x, plus a one-cycle ch1_x_wr pulse
--   0x100C  CH1_Y_OUT       RO   <- ch1_y (live)
--   0x2000  CH2_X_IN        RW   -> ch2_x, plus a one-cycle ch2_x_wr pulse
--   0x200C  CH2_Y_OUT       RO   <- ch2_y (live)
-- Everything else reads 0 and ignores writes (the bus still answers OKAY).
--
-- ADDRESS DECODE uses address bits [13:2]: bits [1:0] are the byte offset and
-- are ignored, and bits [13:12] select the module -- so the generic must be 14.
-- The `13 downto 2` slices below are hard-coded to that width.
--
-- The x_wr pulses are the raw decoded write strobes, not delayed: the filters
-- delay them one clock themselves because X_IN only holds its new value from
-- the cycle after the strobe. Do not add a second delay here.
--
-- Read data is a combinational mux on the latched read address (rd_addr), which
-- is what my_axi expects.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

library work;
use work.axil_pkg.all;

entity csr_top is
  generic (
    C_S_AXI_ADDR_WIDTH : integer := 14
    );
  port (
    aclk    : in  std_logic;
    aresetn : in  std_logic;   -- active LOW
    s_m2s   : in  t_axil_m2s;
    s_s2m   : out t_axil_s2m;
    
    tdm_cfg_reg0     : out std_logic_vector(AXIL_DATA_W-1 downto 0);
    tdm_cfg_reg1     : out std_logic_vector(AXIL_DATA_W-1 downto 0);
    tdm_cfg_reg2     : out std_logic_vector(AXIL_DATA_W-1 downto 0);
    tdm_status       : in  std_logic_vector(AXIL_DATA_W-1 downto 0);

    ch1_x    : out std_logic_vector(AXIL_DATA_W-1 downto 0);
    ch1_x_wr : out std_logic;
    ch1_y    : in  std_logic_vector(AXIL_DATA_W-1 downto 0);
    ch2_x    : out std_logic_vector(AXIL_DATA_W-1 downto 0);
    ch2_x_wr : out std_logic;
    ch2_y    : in  std_logic_vector(AXIL_DATA_W-1 downto 0)
    );
end csr_top;

architecture rtl of csr_top is
  
  component csr_reg is
    port (
      aclk       : in  std_logic;
      aresetn    : in  std_logic;
      wr_en      : in  std_logic;
      wr_data    : in  std_logic_vector(AXIL_DATA_W-1 downto 0);
      wr_strb    : in  std_logic_vector(AXIL_STRB_W-1 downto 0);
      hw_wr_en   : in  std_logic := '0';
      hw_wr_data : in  std_logic_vector(AXIL_DATA_W-1 downto 0) := (others => '0');
      rd_data    : out std_logic_vector(AXIL_DATA_W-1 downto 0)
      );
  end component;

  component my_axi is
    generic (
      C_S_AXI_ADDR_WIDTH : integer := 14
      );
    port (
      aclk    : in  std_logic;
      aresetn : in  std_logic;   -- active LOW
      s_m2s   : in  t_axil_m2s;
      s_s2m   : out t_axil_s2m;

      wr_en   : out std_logic;
      wr_addr : out std_logic_vector(C_S_AXI_ADDR_WIDTH-1 downto 0);
      wr_data : out std_logic_vector(31 downto 0);
      wr_strb : out std_logic_vector(AXIL_STRB_W-1 downto 0);
      
      rd_en   : out std_logic;
      rd_addr : out std_logic_vector(C_S_AXI_ADDR_WIDTH-1 downto 0);
      rd_data : in  std_logic_vector(31 downto 0)    
      );
  end component;

  signal wr_en    : std_logic;
  signal wr_addr  : std_logic_vector(C_S_AXI_ADDR_WIDTH-1 downto 0);
  signal wr_data  : std_logic_vector(AXIL_DATA_W-1 downto 0);
  signal wr_strb  : std_logic_vector(AXIL_STRB_W-1 downto 0);
  signal rd_en    : std_logic;
  signal rd_addr  : std_logic_vector(C_S_AXI_ADDR_WIDTH-1 downto 0);
  signal rd_data  : std_logic_vector(AXIL_DATA_W-1 downto 0);

  subtype  t_word_addr is std_logic_vector(11 downto 0);
  constant A_TDM_N    : t_word_addr := x"000";   -- 0x0000
  constant A_TDM_SHFT : t_word_addr := x"001";   -- 0x0004
  constant A_TDM_CTRL : t_word_addr := x"002";   -- 0x0008
  constant A_TDM_STAT : t_word_addr := x"003";   -- 0x000C
  constant A_CH1_X    : t_word_addr := x"400";   -- 0x1000
  constant A_CH1_Y    : t_word_addr := x"403";   -- 0x100C
  constant A_CH2_X    : t_word_addr := x"800";   -- 0x2000
  constant A_CH2_Y    : t_word_addr := x"803";   -- 0x200C

  signal q_tdm0, q_tdm1, q_tdm2, q_ch1_x, q_ch2_x : std_logic_vector(AXIL_DATA_W-1 downto 0);
  signal sel_tdm0, sel_tdm1, sel_tdm2, sel_ch1_x, sel_ch2_x : std_logic;


begin

  axi : my_axi
    generic map (
      C_S_AXI_ADDR_WIDTH => C_S_AXI_ADDR_WIDTH
      )
  port map (
    aclk    => aclk,
    aresetn => aresetn,
    s_m2s   => s_m2s,
    s_s2m   => s_s2m,

    wr_en   => wr_en,
    wr_addr => wr_addr,
    wr_data => wr_data,
    wr_strb => wr_strb,
    
    rd_en   => rd_en,
    rd_addr => rd_addr,
    rd_data => rd_data
    );

   -- write strobes: bus write AND this register's address
  sel_tdm0  <= '1' when wr_en = '1' and wr_addr(13 downto 2) = A_TDM_N    else '0';
  sel_tdm1  <= '1' when wr_en = '1' and wr_addr(13 downto 2) = A_TDM_SHFT else '0';
  sel_tdm2  <= '1' when wr_en = '1' and wr_addr(13 downto 2) = A_TDM_CTRL else '0';
  sel_ch1_x <= '1' when wr_en = '1' and wr_addr(13 downto 2) = A_CH1_X    else '0';
  sel_ch2_x <= '1' when wr_en = '1' and wr_addr(13 downto 2) = A_CH2_X    else '0';

  reg_tdm0  : csr_reg port map (aclk => aclk, aresetn => aresetn, wr_en => sel_tdm0,  wr_data => wr_data, wr_strb => wr_strb, rd_data => q_tdm0);
  reg_tdm1  : csr_reg port map (aclk => aclk, aresetn => aresetn, wr_en => sel_tdm1,  wr_data => wr_data, wr_strb => wr_strb, rd_data => q_tdm1);
  reg_tdm2  : csr_reg port map (aclk => aclk, aresetn => aresetn, wr_en => sel_tdm2,  wr_data => wr_data, wr_strb => wr_strb, rd_data => q_tdm2);
  reg_ch1_x : csr_reg port map (aclk => aclk, aresetn => aresetn, wr_en => sel_ch1_x, wr_data => wr_data, wr_strb => wr_strb, rd_data => q_ch1_x);
  reg_ch2_x : csr_reg port map (aclk => aclk, aresetn => aresetn, wr_en => sel_ch2_x, wr_data => wr_data, wr_strb => wr_strb, rd_data => q_ch2_x);
  
  tdm_cfg_reg0 <= q_tdm0;
  tdm_cfg_reg1 <= q_tdm1;
  tdm_cfg_reg2 <= q_tdm2;
  ch1_x        <= q_ch1_x;
  ch1_x_wr     <= sel_ch1_x;
  ch2_x        <= q_ch2_x;
  ch2_x_wr     <= sel_ch2_x;

  read_mux : process (rd_addr, q_tdm0, q_tdm1, q_tdm2, tdm_status, q_ch1_x, ch1_y, q_ch2_x, ch2_y)
  begin
    if    rd_addr(13 downto 2) = A_TDM_N    then rd_data <= q_tdm0;
    elsif rd_addr(13 downto 2) = A_TDM_SHFT then rd_data <= q_tdm1;
    elsif rd_addr(13 downto 2) = A_TDM_CTRL then rd_data <= q_tdm2;
    elsif rd_addr(13 downto 2) = A_TDM_STAT then rd_data <= tdm_status;
    elsif rd_addr(13 downto 2) = A_CH1_X    then rd_data <= q_ch1_x;
    elsif rd_addr(13 downto 2) = A_CH1_Y    then rd_data <= ch1_y;
    elsif rd_addr(13 downto 2) = A_CH2_X    then rd_data <= q_ch2_x;
    elsif rd_addr(13 downto 2) = A_CH2_Y    then rd_data <= ch2_y;
    else
      rd_data <= (others => '0');
    end if;
  end process;

  
end rtl;
