library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

entity user_top is
    generic (
        -- Depth of the per-channel state RAM. Sized well past what is used
        -- today on purpose: 64 x 32 bits is ~256 bytes, i.e. free, and it
        -- means growing the channel count never needs a resynthesis.
        MAX_CHANNELS          : integer := 64;
        C_S00_AXI_DATA_WIDTH  : integer := 32;
        C_S00_AXI_ADDR_WIDTH  : integer := 4
    );
    port (
      clk      : in std_logic;
      btn_pl   : in std_logic;
      led_pl_b : out std_logic;
      led_pl_g : out std_logic;
      led_pl_r : out std_logic
      );
end user_top;

architecture rtl of user_top is


  component fpga_top is
    port (
      clk      : in std_logic;
      btn_pl   : in std_logic;
      led_pl_b : out std_logic;
      led_pl_g : out std_logic;
      led_pl_r : out std_logic
      );  
  end component;


begin
  
  fpga_top_led : fpga_top
    port map (
      clk      => clk,
      btn_pl   => btn_pl,
      led_pl_b => led_pl_b,
      led_pl_g => led_pl_g,
      led_pl_r => led_pl_r
    );
  
end rtl;
