----------------------------------------------------------------------------------
-- my_axi: generic AXI4-Lite slave -- 4 CPU-writable registers (slv_reg0-3) plus
-- a read-only "fir_result" hook that reg3 reads back instead of its own value.
-- That hook is how downstream modules (axi_processing_ch1/2, axi_tdm_filter)
-- return output to the CPU without their own AXI logic. slv_reg1/slv_reg2 are
-- often unused -- free runtime-tunable parameter registers (e.g. SHIFT).
--
-- VHDL translation of the former my_axi.v, done so the bus can be an axil_pkg
-- record like everything else. With this converted, the only flat AXI port list
-- left in the design is user_top's entity, which Vivado forces.
--
-- The logic is a deliberate 1:1 port of the Xilinx AXI4-Lite slave template
-- that was there before -- same handshake timing, same aw_en single-outstanding
-- behaviour, same always-OKAY responses, same byte-lane WSTRB writes. It talks
-- to the CPU, so it was translated, not improved. Anything that looks odd
-- (no error path, reg3's aliasing read) was odd before and is load-bearing.
--
-- DATA WIDTH comes from axil_pkg, not a generic: the record fixes it at 32, and
-- a generic that could disagree with the record would be a trap. The address
-- width generic stays -- it is how much of the window this slave decodes.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

library work;
use work.axil_pkg.all;

entity my_axi is
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

        aclk    : in  std_logic;
        aresetn : in  std_logic;   -- active LOW
        s_m2s   : in  t_axil_m2s;
        s_s2m   : out t_axil_s2m
    );
end my_axi;

architecture rtl of my_axi is

    -- ADDR_LSB skips the byte-offset bits (2 for a 32-bit bus); the two bits
    -- above it index the four registers.
    constant ADDR_LSB          : integer := (AXIL_DATA_W/32) + 1;
    constant OPT_MEM_ADDR_BITS : integer := 1;

    subtype t_reg is std_logic_vector(AXIL_DATA_W-1 downto 0);
    subtype t_addr is std_logic_vector(C_S_AXI_ADDR_WIDTH-1 downto 0);

    signal axi_awaddr  : t_addr := (others => '0');
    signal axi_awready : std_logic := '0';
    signal axi_wready  : std_logic := '0';
    signal axi_bresp   : t_axil_resp := (others => '0');
    signal axi_bvalid  : std_logic := '0';
    signal axi_araddr  : t_addr := (others => '0');
    signal axi_arready : std_logic := '0';
    signal axi_rdata   : t_reg := (others => '0');
    signal axi_rresp   : t_axil_resp := (others => '0');
    signal axi_rvalid  : std_logic := '0';
    signal aw_en       : std_logic := '1';

    signal slv_reg0, slv_reg1, slv_reg2, slv_reg3 : t_reg := (others => '0');
    signal slv_reg_wren : std_logic;
    signal slv_reg_rden : std_logic;
    signal reg_data_out : t_reg;

    -- Register index from a latched address.
    function reg_index (a : t_addr) return integer is
    begin
        return to_integer(unsigned(a(ADDR_LSB+OPT_MEM_ADDR_BITS downto ADDR_LSB)));
    end function;

begin

    s_s2m.awready <= axi_awready;
    s_s2m.wready  <= axi_wready;
    s_s2m.bresp   <= axi_bresp;
    s_s2m.bvalid  <= axi_bvalid;
    s_s2m.arready <= axi_arready;
    s_s2m.rdata   <= axi_rdata;
    s_s2m.rresp   <= axi_rresp;
    s_s2m.rvalid  <= axi_rvalid;

    axi_slv_reg_rden <= slv_reg_rden;
    axi_slv_reg_wren <= slv_reg_wren;
    axi_reg_data_out <= reg_data_out;
    axi_slv_reg0     <= slv_reg0;
    axi_slv_reg1     <= slv_reg1;
    axi_slv_reg2     <= slv_reg2;
    axi_slv_reg3     <= slv_reg3;

    -- axi_awready pulses one cycle once both AWVALID and WVALID are seen;
    -- aw_en blocks re-accepting a new address until the prior write's response
    -- has been taken (no outstanding transactions supported).
    aw_accept : process (aclk)
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                axi_awready <= '0';
                aw_en       <= '1';
                axi_awaddr  <= (others => '0');
            else
                if axi_awready = '0' and s_m2s.awvalid = '1'
                   and s_m2s.wvalid = '1' and aw_en = '1' then
                    axi_awready <= '1';
                    aw_en       <= '0';
                    axi_awaddr  <= s_m2s.awaddr(C_S_AXI_ADDR_WIDTH-1 downto 0);
                elsif s_m2s.bready = '1' and axi_bvalid = '1' then
                    aw_en       <= '1';
                    axi_awready <= '0';
                else
                    axi_awready <= '0';
                end if;
            end if;
        end if;
    end process;

    -- axi_wready mirrors axi_awready's timing (same accept condition).
    w_accept : process (aclk)
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                axi_wready <= '0';
            else
                if axi_wready = '0' and s_m2s.wvalid = '1'
                   and s_m2s.awvalid = '1' and aw_en = '1' then
                    axi_wready <= '1';
                else
                    axi_wready <= '0';
                end if;
            end if;
        end if;
    end process;

    slv_reg_wren <= axi_wready and s_m2s.wvalid and axi_awready and s_m2s.awvalid;

    -- Register write, gated by WSTRB per byte lane.
    reg_write : process (aclk)
        variable idx : integer range 0 to 2**(OPT_MEM_ADDR_BITS+1)-1;
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                slv_reg0 <= (others => '0');
                slv_reg1 <= (others => '0');
                slv_reg2 <= (others => '0');
                slv_reg3 <= (others => '0');
            elsif slv_reg_wren = '1' then
                idx := reg_index(axi_awaddr);
                for b in 0 to (AXIL_DATA_W/8)-1 loop
                    if s_m2s.wstrb(b) = '1' then
                        case idx is
                            when 0 => slv_reg0(b*8+7 downto b*8) <= s_m2s.wdata(b*8+7 downto b*8);
                            when 1 => slv_reg1(b*8+7 downto b*8) <= s_m2s.wdata(b*8+7 downto b*8);
                            when 2 => slv_reg2(b*8+7 downto b*8) <= s_m2s.wdata(b*8+7 downto b*8);
                            when 3 => slv_reg3(b*8+7 downto b*8) <= s_m2s.wdata(b*8+7 downto b*8);
                            when others => null;   -- hold
                        end case;
                    end if;
                end loop;
            end if;
        end if;
    end process;

    -- Write response: always OKAY, no error path implemented.
    b_resp : process (aclk)
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                axi_bvalid <= '0';
                axi_bresp  <= (others => '0');
            else
                if axi_awready = '1' and s_m2s.awvalid = '1' and axi_bvalid = '0'
                   and axi_wready = '1' and s_m2s.wvalid = '1' then
                    axi_bvalid <= '1';
                    axi_bresp  <= AXIL_RESP_OKAY;
                elsif s_m2s.bready = '1' and axi_bvalid = '1' then
                    axi_bvalid <= '0';
                end if;
            end if;
        end if;
    end process;

    -- axi_arready pulses one cycle and latches the read address, mirroring the
    -- write-address accept above.
    ar_accept : process (aclk)
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                axi_arready <= '0';
                axi_araddr  <= (others => '0');
            else
                if axi_arready = '0' and s_m2s.arvalid = '1' then
                    axi_arready <= '1';
                    axi_araddr  <= s_m2s.araddr(C_S_AXI_ADDR_WIDTH-1 downto 0);
                else
                    axi_arready <= '0';
                end if;
            end if;
        end if;
    end process;

    -- axi_rvalid pulses once the address is accepted; read data is always OKAY.
    r_valid : process (aclk)
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                axi_rvalid <= '0';
                axi_rresp  <= (others => '0');
            else
                if axi_arready = '1' and s_m2s.arvalid = '1' and axi_rvalid = '0' then
                    axi_rvalid <= '1';
                    axi_rresp  <= AXIL_RESP_OKAY;
                elsif axi_rvalid = '1' and s_m2s.rready = '1' then
                    axi_rvalid <= '0';
                end if;
            end if;
        end if;
    end process;

    slv_reg_rden <= axi_arready and s_m2s.arvalid and (not axi_rvalid);

    read_mux : process (axi_araddr, slv_reg0, slv_reg1, slv_reg2, fir_result)
    begin
        case reg_index(axi_araddr) is
            when 0      => reg_data_out <= slv_reg0;
            when 1      => reg_data_out <= slv_reg1;
            when 2      => reg_data_out <= slv_reg2;
            when 3      => reg_data_out <= fir_result;  -- NOT slv_reg3
            when others => reg_data_out <= (others => '0');
        end case;
    end process;

    r_data : process (aclk)
    begin
        if rising_edge(aclk) then
            if aresetn = '0' then
                axi_rdata <= (others => '0');
            elsif slv_reg_rden = '1' then
                axi_rdata <= reg_data_out;
            end if;
        end if;
    end process;

end rtl;
