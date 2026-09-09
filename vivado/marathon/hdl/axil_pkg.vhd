----------------------------------------------------------------------------------
-- axil_pkg: AXI4-Lite as two records (master-to-slave, slave-to-master) instead
-- of 19 loose signals repeated at every entity.
--
-- PLAIN VHDL-93. Records in ports have always been legal; only *unconstrained*
-- record elements need VHDL-2008, which is why every field here has a fixed
-- width. That matters in this project: 2008 is a per-FILE property in Vivado
-- (file_type), not covered by the project-level enable_vhdl_2008, so avoiding
-- it removes a setting that would silently go missing on a rebuild.
--
-- WHERE RECORDS CANNOT GO:
--   * user_top's entity -- Vivado infers BD interfaces from port NAMES, so the
--     boundary facing the block design must stay flat. Packed into a record on
--     the first line of the architecture and never seen flat again.
--   * my_axi -- it is Verilog, and Verilog has no records. Every my_axi
--     instance needs one flat unpack. Today that is two (ch1, ch2); once the
--     central register file replaces the per-module slaves it is exactly one.
--
-- ADDRESS WIDTH is fixed at 32 so one record type serves every bus regardless
-- of how much of the window a given slave decodes. Slaves take the low bits
-- they care about and ignore the rest -- which is what they already do, since
-- my_axi decodes 4 bits inside a 4K segment.
----------------------------------------------------------------------------------

library IEEE;
use IEEE.STD_LOGIC_1164.ALL;

package axil_pkg is

    constant AXIL_ADDR_W : integer := 32;
    constant AXIL_DATA_W : integer := 32;
    constant AXIL_STRB_W : integer := AXIL_DATA_W/8;

    subtype t_axil_addr is std_logic_vector(AXIL_ADDR_W-1 downto 0);
    subtype t_axil_data is std_logic_vector(AXIL_DATA_W-1 downto 0);
    subtype t_axil_strb is std_logic_vector(AXIL_STRB_W-1 downto 0);
    subtype t_axil_resp is std_logic_vector(1 downto 0);
    subtype t_axil_prot is std_logic_vector(2 downto 0);

    constant AXIL_RESP_OKAY   : t_axil_resp := "00";
    constant AXIL_RESP_DECERR : t_axil_resp := "11";

    -- Master -> slave: everything the master drives.
    type t_axil_m2s is record
        awaddr  : t_axil_addr;
        awprot  : t_axil_prot;
        awvalid : std_logic;
        wdata   : t_axil_data;
        wstrb   : t_axil_strb;
        wvalid  : std_logic;
        bready  : std_logic;
        araddr  : t_axil_addr;
        arprot  : t_axil_prot;
        arvalid : std_logic;
        rready  : std_logic;
    end record;

    -- Slave -> master: everything the slave drives.
    type t_axil_s2m is record
        awready : std_logic;
        wready  : std_logic;
        bresp   : t_axil_resp;
        bvalid  : std_logic;
        arready : std_logic;
        rdata   : t_axil_data;
        rresp   : t_axil_resp;
        rvalid  : std_logic;
    end record;

    -- Unconstrained arrays of a constrained record: also VHDL-93 legal as a
    -- port type, the actual supplies the range. Lets one demux serve any N.
    type t_axil_m2s_array is array (natural range <>) of t_axil_m2s;
    type t_axil_s2m_array is array (natural range <>) of t_axil_s2m;

    constant AXIL_M2S_IDLE : t_axil_m2s := (
        awaddr => (others => '0'), awprot => (others => '0'), awvalid => '0',
        wdata  => (others => '0'), wstrb  => (others => '0'), wvalid  => '0',
        bready => '0',
        araddr => (others => '0'), arprot => (others => '0'), arvalid => '0',
        rready => '0');

    constant AXIL_S2M_IDLE : t_axil_s2m := (
        awready => '0', wready => '0',
        bresp   => AXIL_RESP_OKAY, bvalid => '0',
        arready => '0',
        rdata   => (others => '0'), rresp => AXIL_RESP_OKAY, rvalid => '0');

end package axil_pkg;
