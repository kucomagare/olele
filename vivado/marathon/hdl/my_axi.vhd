----------------------------------------------------------------------------------
-- my_axi: the AXI4-Lite protocol engine, and nothing else. It owns the five
-- channel handshakes and turns them into a small register bus for csr_top:
--
--   write:  wr_en (one-cycle pulse), wr_addr, wr_data, wr_strb
--   read:   rd_en (one-cycle pulse), rd_addr, and rd_data coming back in
--
-- It holds NO registers and knows nothing about what sits behind it. Adding a
-- register never touches this file -- the address decode, the storage (csr_reg)
-- and the read mux all live in csr_top.
--
-- wr_addr / rd_addr are the LATCHED addresses (captured when the address is
-- accepted), valid in the cycle wr_en / rd_en is high. wr_data and wr_strb are
-- the live bus values, held by the master for that same cycle. rd_data must be
-- combinational from rd_addr: it is captured into the read response in the
-- cycle rd_en is high, so a registered read mux would need rvalid delayed too.
--
-- VHDL translation of the former my_axi.v, done so the bus can be an axil_pkg
-- record like everything else.
--
-- The handshake is a deliberate 1:1 port of the Xilinx AXI4-Lite slave
-- template that was there before -- same timing, same aw_en single-outstanding
-- behaviour, same always-OKAY responses. It talks to the CPU, so it was
-- translated, not improved. Anything that looks odd (no error path, every
-- address answers OKAY) was odd before and is load-bearing.
--
-- DATA WIDTH comes from axil_pkg, not a generic: the record fixes it at 32, and
-- a generic that could disagree with the record would be a trap. The address
-- width generic stays -- it is how many address bits are latched and passed on,
-- so it has to cover the whole window csr_top decodes (14 bits = 16K).
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

library work;
use work.axil_pkg.all;

entity my_axi is
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
end my_axi;

architecture rtl of my_axi is

  subtype t_reg is std_logic_vector(AXIL_DATA_W-1 downto 0);
  subtype t_addr is std_logic_vector(C_S_AXI_ADDR_WIDTH-1 downto 0);

  signal axi_awaddr  : t_addr      := (others => '0');
  signal axi_awready : std_logic   := '0';
  signal axi_wready  : std_logic   := '0';
  signal axi_bresp   : t_axil_resp := (others => '0');
  signal axi_bvalid  : std_logic   := '0';
  signal axi_araddr  : t_addr      := (others => '0');
  signal axi_arready : std_logic   := '0';
  signal axi_rdata   : t_reg       := (others => '0');
  signal axi_rresp   : t_axil_resp := (others => '0');
  signal axi_rvalid  : std_logic   := '0';
  signal aw_en       : std_logic   := '1';

  signal slv_reg_wren : std_logic;
  signal slv_reg_rden : std_logic;

begin

  s_s2m.awready <= axi_awready;
  s_s2m.wready  <= axi_wready;
  s_s2m.bresp   <= axi_bresp;
  s_s2m.bvalid  <= axi_bvalid;
  s_s2m.arready <= axi_arready;
  s_s2m.rdata   <= axi_rdata;
  s_s2m.rresp   <= axi_rresp;
  s_s2m.rvalid  <= axi_rvalid;

  wr_en   <= slv_reg_wren;
  wr_addr <= axi_awaddr;
  wr_data <= s_m2s.wdata;
  wr_strb <= s_m2s.wstrb;
             
  rd_en   <= slv_reg_rden;
  rd_addr <= axi_araddr;

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

  r_data : process (aclk)
  begin
    if rising_edge(aclk) then
      if aresetn = '0' then
        axi_rdata <= (others => '0');
      elsif slv_reg_rden = '1' then
        axi_rdata <= rd_data;
      end if;
    end if;
  end process;

end rtl;
