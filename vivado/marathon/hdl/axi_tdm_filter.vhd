----------------------------------------------------------------------------------
-- axi_tdm_filter: TDM streaming IIR low-pass, marathon's replacement for
-- sizif's per-channel AXI-Lite peripherals (axi_processing_ch1/ch2), which
-- put the CPU in the path of every sample.
--
-- ONE arithmetic datapath shared by all channels; per-channel state lives
-- in a RAM indexed by slot. Logic cost doesn't grow with channel count --
-- only the state RAM does (MAX_CHANNELS x 32 bits).
--
-- STREAM FRAME: [ts][ch1]..[chN] [ts][ch1]..[chN] ... one 32-bit slot per
-- beat. Slot 0 = TIMESTAMP, passes through UNFILTERED. Slots 1..N = samples,
-- filtered against state(slot-1). N is a control register, not a bitstream
-- param. tlast marks the DMA BUFFER end, not the frame end -- it
-- resynchronises the slot counter to 0, so the DMA buffer must be a whole
-- number of frames or channel assignment rotates on the next buffer. See
-- research_info/dma-architecture.md ("Ping-pong buffering").
--
-- y[n] = y[n-1] + (x[n]-y[n-1]) >> SHIFT, alpha = 1/2**SHIFT (same as
-- axi_processing_ch1.vhd). SHIFT=0 is an exact bypass (y=x), no special
-- case needed -- a live A/B toggle for filter on/off. The truncating shift
-- has a known, measured fixed-point bias (~-15 counts steady-state at
-- SHIFT=4, dead zone below 2**SHIFT) -- kept deliberately so marathon stays
-- comparable with sizif's output.
--
-- AXI4-LITE REGS (via my_axi.v; reg3 rides the "fir_result" hook):
--   reg0 (0x0,W) N_CHANNELS  -- clamped to MAX_CHANNELS
--   reg1 (0x4,W) SHIFT       -- bits [4:0]; 0 = bypass
--   reg2 (0x8,W) CONTROL     -- bit0 byte-swap, bit1 clear-state
--   reg3 (0xC,R) STATUS      -- [7:0] slot, [15:8] N_CHANNELS,
--                                [20:16] SHIFT, [24] s_tvalid, [25] m_tready
--   Byte-swap does big/little-endian conversion in fabric so the CPU never
--   touches a sample; default OFF so software-swap firmware keeps working.
--   Clear forces state writes to zero and output passthrough; hold one
--   frame to zero every channel.
--
-- FLOW CONTROL: purely combinational tready/tvalid/tlast pass-through with
-- the arithmetic in the same cycle -- ~20ns budget at 50MHz is ample, and
-- no pipeline means tlast can never land on the wrong beat (S2MM hangs
-- forever with no error otherwise -- the nastiest failure mode here). If a
-- deeper filter is needed later, TDM gives N cycles of settling time for
-- free -- register the datapath and delay tvalid/tlast by the same stages.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;
use IEEE.NUMERIC_STD.ALL;

entity axi_tdm_filter is
    generic (
        -- Depth of the per-channel state RAM. Sized well past what is used
        -- today on purpose: 64 x 32 bits is ~256 bytes, i.e. free, and it
        -- means growing the channel count never needs a resynthesis.
        MAX_CHANNELS          : integer := 64;
        C_S00_AXI_DATA_WIDTH  : integer := 32;
        C_S00_AXI_ADDR_WIDTH  : integer := 4
    );
    port (
        -- AXI4-Lite slave: control/status only, no sample data
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
        s00_axi_rready  : in  std_logic;

        -- AXI4-Stream slave: samples in, from the DMA's MM2S channel
        s_axis_tdata    : in  std_logic_vector(31 downto 0);
        s_axis_tvalid   : in  std_logic;
        s_axis_tready   : out std_logic;
        s_axis_tlast    : in  std_logic;

        -- AXI4-Stream master: samples out, to the DMA's S2MM channel
        m_axis_tdata    : out std_logic_vector(31 downto 0);
        m_axis_tvalid   : out std_logic;
        m_axis_tready   : in  std_logic;
        m_axis_tlast    : out std_logic
    );
end axi_tdm_filter;

architecture rtl of axi_tdm_filter is

    -- reg3 rides its STATUS word on the "fir_result" hook (axi_processing_ch1/2
    -- ride their filter output on the same hook).
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

    -- Byte-reverse a 32-bit word (big-endian <-> little-endian). Pure
    -- rewiring: costs no logic at all, which is the whole point of moving
    -- the swap off the CPU.
    function bswap32 (x : std_logic_vector(31 downto 0))
        return std_logic_vector is
    begin
        return x(7 downto 0) & x(15 downto 8) & x(23 downto 16) & x(31 downto 24);
    end function;

    signal cfg_reg0 : std_logic_vector(C_S00_AXI_DATA_WIDTH-1 downto 0);
    signal cfg_reg1 : std_logic_vector(C_S00_AXI_DATA_WIDTH-1 downto 0);
    signal cfg_reg2 : std_logic_vector(C_S00_AXI_DATA_WIDTH-1 downto 0);
    signal status   : std_logic_vector(C_S00_AXI_DATA_WIDTH-1 downto 0);

    -- Per-channel filter state. Initialised here rather than reset: the
    -- init value is loaded at configuration time on Xilinx parts, and
    -- keeping reset off the array is what lets it infer as distributed
    -- RAM instead of 2048 flip-flops.
    type state_array_t is array (0 to MAX_CHANNELS-1)
        of signed(31 downto 0);
    signal state : state_array_t := (others => (others => '0'));
    attribute ram_style : string;
    attribute ram_style of state : signal is "distributed";

    -- Slot 0 = timestamp, slots 1..n_channels = samples. Needs one extra
    -- bit of range over MAX_CHANNELS because it counts 0..N inclusive.
    signal slot_idx : integer range 0 to MAX_CHANNELS := 0;

    signal n_channels  : integer range 0 to MAX_CHANNELS;
    signal shift_amt   : integer range 0 to 31;
    signal swap_en     : std_logic;
    signal clear_state : std_logic;

    signal beat        : std_logic;   -- a beat actually transfers this cycle
    signal is_ts_slot  : std_logic;

    signal x_native    : std_logic_vector(31 downto 0);
    signal y_prev      : signed(31 downto 0);
    signal y_new       : signed(31 downto 0);
    signal result      : std_logic_vector(31 downto 0);

begin

    my_axi_inst : my_axi
        generic map (
            C_S_AXI_DATA_WIDTH => C_S00_AXI_DATA_WIDTH,
            C_S_AXI_ADDR_WIDTH => C_S00_AXI_ADDR_WIDTH
        )
        port map (
            axi_slv_reg_rden => open,
            axi_slv_reg_wren => open,
            axi_reg_data_out => open,
            axi_slv_reg0     => cfg_reg0,
            axi_slv_reg1     => cfg_reg1,
            axi_slv_reg2     => cfg_reg2,
            axi_slv_reg3     => open,
            fir_result       => status,
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

    -- ---------------- control register decode ----------------
    -- Clamped so a bad register write can never index past the state RAM.
    n_channels <= MAX_CHANNELS
                  when unsigned(cfg_reg0) > to_unsigned(MAX_CHANNELS, 32)
                  else to_integer(unsigned(cfg_reg0(7 downto 0)));
    shift_amt   <= to_integer(unsigned(cfg_reg1(4 downto 0)));
    swap_en     <= cfg_reg2(0);
    clear_state <= cfg_reg2(1);

    status(7 downto 0)   <= std_logic_vector(to_unsigned(slot_idx, 8));
    status(15 downto 8)  <= std_logic_vector(to_unsigned(n_channels, 8));
    status(20 downto 16) <= std_logic_vector(to_unsigned(shift_amt, 5));
    status(23 downto 21) <= (others => '0');
    status(24)           <= s_axis_tvalid;
    status(25)           <= m_axis_tready;
    status(31 downto 26) <= (others => '0');

    -- ---------------- stream handshake ----------------
    -- Straight pass-through: this block never stalls of its own accord, so
    -- backpressure from S2MM propagates directly back to MM2S.
    s_axis_tready <= m_axis_tready;
    m_axis_tvalid <= s_axis_tvalid;
    m_axis_tlast  <= s_axis_tlast;

    beat       <= s_axis_tvalid and m_axis_tready;
    is_ts_slot <= '1' when slot_idx = 0 else '0';

    -- ---------------- datapath ----------------
    x_native <= bswap32(s_axis_tdata) when swap_en = '1' else s_axis_tdata;

    -- Slot 0 has no state entry; index 0 is a harmless dummy read.
    y_prev <= state(slot_idx - 1) when slot_idx > 0 else (others => '0');
    y_new  <= y_prev + shift_right(signed(x_native) - y_prev, shift_amt);

    -- Timestamp passes through byte-for-byte (swapping twice would be a
    -- no-op anyway). Clear forces passthrough so the filter's effect can be
    -- switched out without disturbing the stream.
    result <= s_axis_tdata
                  when (is_ts_slot = '1' or clear_state = '1')
              else bswap32(std_logic_vector(y_new)) when swap_en = '1'
              else std_logic_vector(y_new);

    m_axis_tdata <= result;

    -- ---------------- sequential state ----------------
    process (s00_axi_aclk)
    begin
        if rising_edge(s00_axi_aclk) then
            if s00_axi_aresetn = '0' then
                slot_idx <= 0;
            elsif beat = '1' then
                -- State update: only channel slots carry state.
                if slot_idx > 0 then
                    if clear_state = '1' then
                        state(slot_idx - 1) <= (others => '0');
                    else
                        state(slot_idx - 1) <= y_new;
                    end if;
                end if;

                -- Slot counter. tlast resynchronises to slot 0 so a buffer
                -- boundary can never leave the counter mid-frame; the
                -- n_channels wrap does the same at every frame boundary.
                if s_axis_tlast = '1' or slot_idx >= n_channels then
                    slot_idx <= 0;
                else
                    slot_idx <= slot_idx + 1;
                end if;
            end if;
        end if;
    end process;

end rtl;
