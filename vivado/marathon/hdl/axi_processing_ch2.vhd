----------------------------------------------------------------------------------
-- axi_processing_ch2: ch2's filter chain, deliberately its own file/entity
-- (not ch1's instantiated twice) -- currently identical to ch1. In marathon
-- this is the legacy per-sample AXI-Lite path, live A/B'd against
-- axi_tdm_filter.vhd via firmware's comm_use_dma flag, and the automatic
-- fallback if DMA init fails -- NOT dead code.
-- Single-pole IIR low-pass: y[n] = y[n-1] + (x[n]-y[n-1]) >> SHIFT, alpha =
-- 1/2**SHIFT, no multiplier. AXI4-Lite via my_axi.v (mixed-language inst):
-- reg0 (0x0,W) = x[n], reg3 (0xC,R) = y[n] via my_axi's "fir_result" hook.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

library work;
use work.axil_pkg.all;

entity axi_processing_ch2 is
  generic (
    SHIFT                 : integer := 4;
    C_S00_AXI_DATA_WIDTH  : integer := 32;
    C_S00_AXI_ADDR_WIDTH  : integer := 4
    );
  port (
    -- AXI4-Lite slave as an axil_pkg record pair. Only the low
    -- C_S00_AXI_ADDR_WIDTH address bits are decoded; the rest alias.
    aclk    : in  std_logic;
    aresetn : in  std_logic;
    s_m2s   : in  t_axil_m2s;
    s_s2m   : out t_axil_s2m
    );
end axi_processing_ch2;

architecture rtl of axi_processing_ch2 is

  -- reg3 reads "fir_result" (filter output), not its own value -- see my_axi.vhd.
  component my_axi is
    generic (
      C_S_AXI_ADDR_WIDTH : integer := 4
      );
    port (
      axi_slv_reg_rden : out std_logic;
      axi_slv_reg_wren : out std_logic;
      axi_reg_data_out : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      axi_slv_reg0     : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      axi_slv_reg1     : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      axi_slv_reg2     : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      axi_slv_reg3     : out std_logic_vector(AXIL_DATA_W-1 downto 0);
      fir_result       : in  std_logic_vector(AXIL_DATA_W-1 downto 0);
      aclk             : in  std_logic;
      aresetn          : in  std_logic;
      s_m2s            : in  t_axil_m2s;
      s_s2m            : out t_axil_s2m
      );
  end component;

  signal axi_slv_reg_wren : std_logic;
  signal axi_slv_reg0     : std_logic_vector(C_S00_AXI_DATA_WIDTH-1 downto 0);
  signal filtered_result  : std_logic_vector(C_S00_AXI_DATA_WIDTH-1 downto 0);

  signal wren_del : std_logic := '0';
  signal y_reg    : signed(C_S00_AXI_DATA_WIDTH-1 downto 0) := (others => '0');

begin

  my_axi_inst : my_axi
    generic map (
      C_S_AXI_ADDR_WIDTH => C_S00_AXI_ADDR_WIDTH
      )
    port map (
      axi_slv_reg_rden => open,
      axi_slv_reg_wren => axi_slv_reg_wren,
      axi_reg_data_out => open,
      axi_slv_reg0     => axi_slv_reg0,
      axi_slv_reg1     => open,
      axi_slv_reg2     => open,
      axi_slv_reg3     => open,
      fir_result       => filtered_result,
      aclk             => aclk,
      aresetn          => aresetn,
      s_m2s            => s_m2s,
      s_s2m            => s_s2m
      );

  -- Delay wren by one clock: axi_slv_reg0 only reflects a just-written
  -- value the cycle *after* axi_slv_reg_wren pulses (same-edge update).
  process(aclk)
  begin
    if rising_edge(aclk) then
      if aresetn = '0' then
        wren_del <= '0';
      else
        wren_del <= axi_slv_reg_wren;
      end if;
    end if;
  end process;

  -- y[n] = y[n-1] + (x[n] - y[n-1]) >> SHIFT
  process(aclk)
    variable x_in : signed(C_S00_AXI_DATA_WIDTH-1 downto 0);
    variable diff : signed(C_S00_AXI_DATA_WIDTH-1 downto 0);
  begin
    if rising_edge(aclk) then
      if aresetn = '0' then
        y_reg <= (others => '0');
      elsif wren_del = '1' then
        x_in := signed(axi_slv_reg0);
        diff := x_in - y_reg;
        y_reg <= y_reg + shift_right(diff, SHIFT);
      end if;
    end if;
  end process;

  filtered_result <= std_logic_vector(y_reg);

end rtl;
