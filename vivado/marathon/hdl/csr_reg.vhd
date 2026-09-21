----------------------------------------------------------------------------------
-- csr_reg: one 32-bit control/status register. csr_top instantiates one per
-- writable register and decodes the address; this module never sees an address.
--
--   wr_en       this register's write strobe (csr_top: bus write AND address
--               match). The write honours wr_strb per byte lane, so a partial
--               write leaves the other lanes untouched.
--   hw_wr_en    optional hardware write of the whole word. WINS over a CPU write
--               in the same cycle -- swap the two branches in the process if a
--               register ever needs the CPU to win. Leave both hw_wr_* ports
--               unconnected (default '0') for a plain CPU-written register.
--   rd_data     the stored value, read back by csr_top's read mux and fanned
--               out to whatever uses it.
--
-- Synchronous, active-low reset to 0. Every config register therefore boots as
-- 0: SHIFT=0 is bypass, N_CHANNELS=0 is no channels -- firmware relies on that.
--
-- Read-only values (STATUS, filter results) do not use this module: they are
-- live signals wired straight into csr_top's read mux.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

library work;
use work.axil_pkg.all;

entity csr_reg is
  port (
    aclk       : in  std_logic;
    aresetn    : in  std_logic;
    wr_en      : in  std_logic;
    wr_data    : in  std_logic_vector(31 downto 0);
    wr_strb    : in  std_logic_vector(AXIL_STRB_W-1 downto 0);
    hw_wr_en   : in  std_logic                     := '0';
    hw_wr_data : in  std_logic_vector(31 downto 0) := (others => '0');
    rd_data    : out std_logic_vector(31 downto 0)
    );
end csr_reg;

architecture rtl of csr_reg is
begin

  process (aclk)
  begin
    if rising_edge(aclk) then
      if aresetn = '0' then
        rd_data <= (others => '0');
      elsif hw_wr_en = '1' then
        -- hardware write: whole word, wins over a simultaneous CPU write
        rd_data <= hw_wr_data;
      elsif wr_en = '1' then
        for b in 0 to (AXIL_DATA_W/8)-1 loop
          if wr_strb(b) = '1' then
            rd_data(b*8+7 downto b*8) <= wr_data(b*8+7 downto b*8);
          end if;
        end loop;
      end if;
    end if;
  end process;

end rtl;
