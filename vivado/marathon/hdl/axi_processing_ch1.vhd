----------------------------------------------------------------------------------
-- axi_processing_ch1: ch1's filter chain, deliberately its own file/entity
-- (not ch2's instantiated twice) so each channel can diverge independently.
-- In marathon this is the legacy per-sample AXI-Lite path, live A/B'd
-- against axi_tdm_filter.vhd via firmware's comm_use_dma flag, and the
-- automatic fallback if DMA init fails -- NOT dead code.
-- Single-pole IIR low-pass: y[n] = y[n-1] + (x[n]-y[n-1]) >> SHIFT, alpha =
-- 1/2**SHIFT, no multiplier. AXI4-Lite via my_axi.v (mixed-language inst):
-- reg0 (0x0,W) = x[n], reg3 (0xC,R) = y[n] via my_axi's "fir_result" hook.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

entity axi_processing_ch1 is
    generic (
        SHIFT                 : integer := 4;
        C_S00_AXI_DATA_WIDTH  : integer := 32;
        C_S00_AXI_ADDR_WIDTH  : integer := 4
    );
    port (
        -- AXI4-Lite slave interface
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
end axi_processing_ch1;

architecture rtl of axi_processing_ch1 is

    -- reg3 reads "fir_result" (filter output), not its own value -- see my_axi.v.
    component my_axi is
        generic (
            C_S_AXI_DATA_WIDTH : integer := 32;
            C_S_AXI_ADDR_WIDTH : integer := 4
        );
        port (
            axi_slv_reg_rden : out std_logic;
            axi_slv_reg_wren : out std_logic;
            axi_reg_data_out : out std_logic_vector(C_S_AXI_DATA_WIDTH-1 downto 0);
            axi_slv_reg0     : out std_logic_vector(C_S_AXI_DATA_WIDTH-1 downto 0);
            axi_slv_reg1     : out std_logic_vector(C_S_AXI_DATA_WIDTH-1 downto 0);
            axi_slv_reg2     : out std_logic_vector(C_S_AXI_DATA_WIDTH-1 downto 0);
            axi_slv_reg3     : out std_logic_vector(C_S_AXI_DATA_WIDTH-1 downto 0);
            fir_result       : in  std_logic_vector(C_S_AXI_DATA_WIDTH-1 downto 0);
            S_AXI_ACLK       : in  std_logic;
            S_AXI_ARESETN    : in  std_logic;
            S_AXI_AWADDR     : in  std_logic_vector(C_S_AXI_ADDR_WIDTH-1 downto 0);
            S_AXI_AWPROT     : in  std_logic_vector(2 downto 0);
            S_AXI_AWVALID    : in  std_logic;
            S_AXI_AWREADY    : out std_logic;
            S_AXI_WDATA      : in  std_logic_vector(C_S_AXI_DATA_WIDTH-1 downto 0);
            S_AXI_WSTRB      : in  std_logic_vector((C_S_AXI_DATA_WIDTH/8)-1 downto 0);
            S_AXI_WVALID     : in  std_logic;
            S_AXI_WREADY     : out std_logic;
            S_AXI_BRESP      : out std_logic_vector(1 downto 0);
            S_AXI_BVALID     : out std_logic;
            S_AXI_BREADY     : in  std_logic;
            S_AXI_ARADDR     : in  std_logic_vector(C_S_AXI_ADDR_WIDTH-1 downto 0);
            S_AXI_ARPROT     : in  std_logic_vector(2 downto 0);
            S_AXI_ARVALID    : in  std_logic;
            S_AXI_ARREADY    : out std_logic;
            S_AXI_RDATA      : out std_logic_vector(C_S_AXI_DATA_WIDTH-1 downto 0);
            S_AXI_RRESP      : out std_logic_vector(1 downto 0);
            S_AXI_RVALID     : out std_logic;
            S_AXI_RREADY     : in  std_logic
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
            C_S_AXI_DATA_WIDTH => C_S00_AXI_DATA_WIDTH,
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
            S_AXI_ACLK       => s00_axi_aclk,
            S_AXI_ARESETN    => s00_axi_aresetn,
            S_AXI_AWADDR     => s00_axi_awaddr,
            S_AXI_AWPROT     => s00_axi_awprot,
            S_AXI_AWVALID    => s00_axi_awvalid,
            S_AXI_AWREADY    => s00_axi_awready,
            S_AXI_WDATA      => s00_axi_wdata,
            S_AXI_WSTRB      => s00_axi_wstrb,
            S_AXI_WVALID     => s00_axi_wvalid,
            S_AXI_WREADY     => s00_axi_wready,
            S_AXI_BRESP      => s00_axi_bresp,
            S_AXI_BVALID     => s00_axi_bvalid,
            S_AXI_BREADY     => s00_axi_bready,
            S_AXI_ARADDR     => s00_axi_araddr,
            S_AXI_ARPROT     => s00_axi_arprot,
            S_AXI_ARVALID    => s00_axi_arvalid,
            S_AXI_ARREADY    => s00_axi_arready,
            S_AXI_RDATA      => s00_axi_rdata,
            S_AXI_RRESP      => s00_axi_rresp,
            S_AXI_RVALID     => s00_axi_rvalid,
            S_AXI_RREADY     => s00_axi_rready
        );

    -- Delay wren by one clock: axi_slv_reg0 only reflects a just-written
    -- value the cycle *after* axi_slv_reg_wren pulses (same-edge update).
    process(s00_axi_aclk)
    begin
        if rising_edge(s00_axi_aclk) then
            if s00_axi_aresetn = '0' then
                wren_del <= '0';
            else
                wren_del <= axi_slv_reg_wren;
            end if;
        end if;
    end process;

    -- y[n] = y[n-1] + (x[n] - y[n-1]) >> SHIFT
    process(s00_axi_aclk)
        variable x_in : signed(C_S00_AXI_DATA_WIDTH-1 downto 0);
        variable diff : signed(C_S00_AXI_DATA_WIDTH-1 downto 0);
    begin
        if rising_edge(s00_axi_aclk) then
            if s00_axi_aresetn = '0' then
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
