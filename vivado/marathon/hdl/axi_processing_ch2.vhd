----------------------------------------------------------------------------------
-- axi_processing_ch2: ch2's filter chain, deliberately its own file/entity
-- (not ch1's instantiated twice) -- currently identical to ch1. In marathon
-- this is the legacy per-sample AXI-Lite path, live A/B'd against
-- axi_tdm_filter.vhd via firmware's comm_use_dma flag, and the automatic
-- fallback if DMA init fails -- NOT dead code.
-- Single-pole IIR low-pass: y[n] = y[n-1] + (x[n]-y[n-1]) >> SHIFT, alpha =
-- 1/2**SHIFT, no multiplier.
--
-- No bus logic in here. csr_top owns the register at 0x2000 (X_IN) and hands
-- this module its value plus a one-cycle write pulse; the result goes back to
-- csr_top, which serves it at 0x200C (Y_OUT, read-only). Firmware writes x[n],
-- then reads y[n] -- no polling, the result settles well inside one bus round
-- trip.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

library work;
use work.axil_pkg.all;

entity axi_processing_ch2 is
  generic (
    SHIFT : integer := 4
    );
  port (
    aclk             : in  std_logic;
    aresetn          : in  std_logic;

    -- From csr_top. wren pulses for one cycle when X_IN is written; reg0 is
    -- X_IN itself and only holds the new value from the following cycle.
    axi_slv_reg_wren : in  std_logic;
    axi_slv_reg0     : in  std_logic_vector(31 downto 0);

    -- To csr_top: y[n], read back at Y_OUT.
    filtered_result  : out std_logic_vector(31 downto 0)
    );
end axi_processing_ch2;

architecture rtl of axi_processing_ch2 is

  signal wren_del : std_logic := '0';
  signal y_reg    : signed(31 downto 0) := (others => '0');

begin

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
    variable x_in : signed(31 downto 0);
    variable diff : signed(31 downto 0);
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
