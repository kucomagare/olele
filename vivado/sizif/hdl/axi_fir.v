`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 04/08/2026 10:24:52 PM
// Design Name: 
// Module Name: axi_fir
// Project Name: 
// Target Devices: 
// Tool Versions: 
// Description: 
// 
// Dependencies: 
// 
// Revision:
// Revision 0.01 - File Created
// Additional Comments:
// 
//////////////////////////////////////////////////////////////////////////////////

module axi_fir #(
		// Users to add parameters here
        parameter integer FIR_DEPTH=29,
		// User parameters ends
		// Do not modify the parameters beyond this line

		// Parameters of Axi Slave Bus Interface S00_AXI
		parameter integer C_S00_AXI_DATA_WIDTH	= 32,
		parameter integer C_S00_AXI_ADDR_WIDTH	= 4

	)(
		// Users to add ports here
        output wire axi_slv_reg_rden,
        output wire axi_slv_reg_wren,
        output wire [C_S00_AXI_DATA_WIDTH-1:0] axi_reg_data_out,
        output wire [C_S00_AXI_DATA_WIDTH-1:0] axi_slv_reg0,
        output wire [C_S00_AXI_DATA_WIDTH-1:0] axi_slv_reg1,
        output wire [C_S00_AXI_DATA_WIDTH-1:0] axi_slv_reg2,
        output wire [C_S00_AXI_DATA_WIDTH-1:0] axi_slv_reg3,
        output wire [1:0] int_count,
        output wire [36:0] processed_data,
		// User ports ends
		// Do not modify the ports beyond this line


		// Ports of Axi Slave Bus Interface S00_AXI
		input wire  s00_axi_aclk,
		input wire  s00_axi_aresetn,
		input wire [C_S00_AXI_ADDR_WIDTH-1 : 0] s00_axi_awaddr,
		input wire [2 : 0] s00_axi_awprot,
		input wire  s00_axi_awvalid,
		output wire  s00_axi_awready,
		input wire [C_S00_AXI_DATA_WIDTH-1 : 0] s00_axi_wdata,
		input wire [(C_S00_AXI_DATA_WIDTH/8)-1 : 0] s00_axi_wstrb,
		input wire  s00_axi_wvalid,
		output wire  s00_axi_wready,
		output wire [1 : 0] s00_axi_bresp,
		output wire  s00_axi_bvalid,
		input wire  s00_axi_bready,
		input wire [C_S00_AXI_ADDR_WIDTH-1 : 0] s00_axi_araddr,
		input wire [2 : 0] s00_axi_arprot,
		input wire  s00_axi_arvalid,
		output wire  s00_axi_arready,
		output wire [C_S00_AXI_DATA_WIDTH-1 : 0] s00_axi_rdata,
		output wire [1 : 0] s00_axi_rresp,
		output wire  s00_axi_rvalid,
		input wire  s00_axi_rready

	);
// Instantiation of Axi Bus Interface S00_AXI
	my_axi # ( 
		.C_S_AXI_DATA_WIDTH(C_S00_AXI_DATA_WIDTH),
		.C_S_AXI_ADDR_WIDTH(C_S00_AXI_ADDR_WIDTH)
	) my_axi_inst (
		.S_AXI_ACLK(s00_axi_aclk),
		.S_AXI_ARESETN(s00_axi_aresetn),
		.S_AXI_AWADDR(s00_axi_awaddr),
		.S_AXI_AWPROT(s00_axi_awprot),
		.S_AXI_AWVALID(s00_axi_awvalid),
		.S_AXI_AWREADY(s00_axi_awready),
		.S_AXI_WDATA(s00_axi_wdata),
		.S_AXI_WSTRB(s00_axi_wstrb),
		.S_AXI_WVALID(s00_axi_wvalid),
		.S_AXI_WREADY(s00_axi_wready),
		.S_AXI_BRESP(s00_axi_bresp),
		.S_AXI_BVALID(s00_axi_bvalid),
		.S_AXI_BREADY(s00_axi_bready),
		.S_AXI_ARADDR(s00_axi_araddr),
		.S_AXI_ARPROT(s00_axi_arprot),
		.S_AXI_ARVALID(s00_axi_arvalid),
		.S_AXI_ARREADY(s00_axi_arready),
		.S_AXI_RDATA(s00_axi_rdata),
		.S_AXI_RRESP(s00_axi_rresp),
		.S_AXI_RVALID(s00_axi_rvalid),
		.S_AXI_RREADY(s00_axi_rready),
		// User defined ports connection
        .axi_slv_reg_rden(axi_slv_reg_rden),
        .axi_slv_reg_wren(axi_slv_reg_wren),
        .axi_reg_data_out(axi_reg_data_out),
        .axi_slv_reg0(axi_slv_reg0),
        .axi_slv_reg1(axi_slv_reg1),
        .axi_slv_reg2(axi_slv_reg2),
        .axi_slv_reg3(axi_slv_reg3),
        //.fir_result(fir_sum[36:5])
        .fir_result(fir_sum[31:0])
	);


	// Add user logic here
    reg [1:0] clk_counter;
    reg wren_one_clk_del;
    wire signed [15:0] fir_coeff[FIR_DEPTH-1:0];
    reg signed [15:0] fir_in_line [FIR_DEPTH-1:0];
    reg signed [31:0] fir_accum [FIR_DEPTH-1:0];
    reg signed [36:0] fir_sum;
    
    
    // FIR coefitients assignment
    assign fir_coeff[0] = 16'hffaa;
    assign fir_coeff[1] = 16'hff5d;
    assign fir_coeff[2] = 16'hff0d;
    assign fir_coeff[3] = 16'hfed5;
    assign fir_coeff[4] = 16'hfedd;
    assign fir_coeff[5] = 16'hff4a;
    assign fir_coeff[6] = 16'h003c;
    assign fir_coeff[7] = 16'h00ea;
    assign fir_coeff[8] = 16'h02b8;
    assign fir_coeff[9] = 16'h0829;
    assign fir_coeff[10] = 16'h0f23;
    assign fir_coeff[11] = 16'h0ea2;
    assign fir_coeff[12] = 16'h000e;
    assign fir_coeff[13] = 16'heacf;
    assign fir_coeff[14] = 16'h608d;
    assign fir_coeff[15] = 16'heacf;
    assign fir_coeff[16] = 16'h000e;
    assign fir_coeff[17] = 16'h0ea2;
    assign fir_coeff[18] = 16'h0f23;
    assign fir_coeff[19] = 16'h0829;
    assign fir_coeff[20] = 16'h02b8;
    assign fir_coeff[21] = 16'h00ea;
    assign fir_coeff[22] = 16'h003c;
    assign fir_coeff[23] = 16'hff4a;
    assign fir_coeff[24] = 16'hfedd;
    assign fir_coeff[25] = 16'hfed5;
    assign fir_coeff[26] = 16'hff0d;
    assign fir_coeff[27] = 16'hff5d;
    assign fir_coeff[28] = 16'hffaa;
    
    // counter for observing the clock with ILA
    always @(posedge s00_axi_aclk)
        if(s00_axi_aresetn)
            clk_counter <= clk_counter + 2'b01;
        else
            clk_counter <= 2'b00;
            
    assign int_count = clk_counter;
    
    // Delaying the write en signal with one lock
    always @(posedge s00_axi_aclk)
        if(axi_slv_reg_wren & s00_axi_aresetn) 
            wren_one_clk_del <= 1'b1;
        else 
            wren_one_clk_del <= 1'b0;
    
    // creating the input tap line for the FIR

/*
always @(posedge s00_axi_aclk)
    if(s00_axi_aresetn) begin
        fir_in_line[0] <= 0;       
    end else
        if(wren_one_clk_del)
            fir_in_line[0] <= axi_slv_reg0;
        else 
            fir_in_line[0] <= fir_in_line[0];    

generate genvar k;
for(k = 1; k < FIR_DEPTH ; k = k+1) begin: gen_tap
always @(posedge s00_axi_aclk) begin
    if(!s00_axi_aresetn) begin 
        fir_in_line[k] <= 0;
    end else
        if(wren_one_clk_del) 
            fir_in_line[k] <= fir_in_line[k-1];
        else
            fir_in_line[k] <= fir_in_line[k];
end
end 
endgenerate
*/
 /*           
    // Create the multiplier line of the FIR
  
generate genvar j;  
for(j = 0; j < FIR_DEPTH ; j = j+1) begin: for1
always @(posedge s00_axi_aclk) begin
    if(s00_axi_aresetn) begin
        if(wren_one_clk_del) begin
            fir_accum[j] <= fir_in_line[j] * fir_coeff[j];
        end
    end else begin
        fir_accum[j] <= 0;
    end
end
end
endgenerate
*/

// creating the input tap line for the FIR
always @(posedge s00_axi_aclk) begin
    if(!s00_axi_aresetn) begin 
        fir_in_line[0] <= 0;
        fir_in_line[1] <= 0;
        fir_in_line[2] <= 0;
        fir_in_line[3] <= 0;
        fir_in_line[4] <= 0;
        fir_in_line[5] <= 0;
        fir_in_line[6] <= 0;
        fir_in_line[7] <= 0;
        fir_in_line[8] <= 0;
        fir_in_line[9] <= 0;
        fir_in_line[10] <= 0;
        fir_in_line[11] <= 0;
        fir_in_line[12] <= 0;
        fir_in_line[13] <= 0;
        fir_in_line[14] <= 0;
        fir_in_line[15] <= 0;
        fir_in_line[16] <= 0;
        fir_in_line[17] <= 0;
        fir_in_line[18] <= 0;
        fir_in_line[19] <= 0;
        fir_in_line[20] <= 0;
        fir_in_line[21] <= 0;
        fir_in_line[22] <= 0;
        fir_in_line[23] <= 0;
        fir_in_line[24] <= 0;
        fir_in_line[25] <= 0;
        fir_in_line[26] <= 0;
        fir_in_line[27] <= 0;
        fir_in_line[28] <= 0;
     end else begin
         if(wren_one_clk_del) begin
             fir_in_line[0] <= axi_slv_reg0[23:8];
             fir_in_line[1] <= fir_in_line[0];
             fir_in_line[2] <= fir_in_line[1];
             fir_in_line[3] <= fir_in_line[2];
             fir_in_line[4] <= fir_in_line[3];
             fir_in_line[5] <= fir_in_line[4];
             fir_in_line[6] <= fir_in_line[5];
             fir_in_line[7] <= fir_in_line[6];
             fir_in_line[8] <= fir_in_line[7];
             fir_in_line[9] <= fir_in_line[8];
             fir_in_line[10] <= fir_in_line[9];
             fir_in_line[11] <= fir_in_line[10];
             fir_in_line[12] <= fir_in_line[11];
             fir_in_line[13] <= fir_in_line[12];
             fir_in_line[14] <= fir_in_line[13];
             fir_in_line[15] <= fir_in_line[14];
             fir_in_line[16] <= fir_in_line[15];
             fir_in_line[17] <= fir_in_line[16];
             fir_in_line[18] <= fir_in_line[17];
             fir_in_line[19] <= fir_in_line[18];
             fir_in_line[20] <= fir_in_line[19];
             fir_in_line[21] <= fir_in_line[20];
             fir_in_line[22] <= fir_in_line[21];
             fir_in_line[23] <= fir_in_line[22];
             fir_in_line[24] <= fir_in_line[23];
             fir_in_line[25] <= fir_in_line[24];
             fir_in_line[26] <= fir_in_line[25];
             fir_in_line[27] <= fir_in_line[26];
             fir_in_line[28] <= fir_in_line[27];
         end else begin 
             fir_in_line[0] <= fir_in_line[0];
             fir_in_line[1] <= fir_in_line[1];
             fir_in_line[2] <= fir_in_line[2];
             fir_in_line[3] <= fir_in_line[3];
             fir_in_line[4] <= fir_in_line[4];
             fir_in_line[5] <= fir_in_line[5];
             fir_in_line[6] <= fir_in_line[6];
             fir_in_line[7] <= fir_in_line[7];
             fir_in_line[8] <= fir_in_line[8];
             fir_in_line[9] <= fir_in_line[9];
             fir_in_line[10] <= fir_in_line[10];
             fir_in_line[11] <= fir_in_line[11];
             fir_in_line[12] <= fir_in_line[12];
             fir_in_line[13] <= fir_in_line[13];
             fir_in_line[14] <= fir_in_line[14];
             fir_in_line[15] <= fir_in_line[15];
             fir_in_line[16] <= fir_in_line[16];
             fir_in_line[17] <= fir_in_line[17];
             fir_in_line[18] <= fir_in_line[18];
             fir_in_line[19] <= fir_in_line[19];
             fir_in_line[20] <= fir_in_line[20];
             fir_in_line[21] <= fir_in_line[21];
             fir_in_line[22] <= fir_in_line[22];
             fir_in_line[23] <= fir_in_line[23];
             fir_in_line[24] <= fir_in_line[24];
             fir_in_line[25] <= fir_in_line[25];
             fir_in_line[26] <= fir_in_line[26];
             fir_in_line[27] <= fir_in_line[27];
             fir_in_line[28] <= fir_in_line[28];
         end
      end
end

// Create the multiplier line of the FIR

always @(posedge s00_axi_aclk) begin
    if(!s00_axi_aresetn) begin
        fir_accum[0] <= 0;
        fir_accum[1] <= 0;
        fir_accum[2] <= 0;
        fir_accum[3] <= 0;
        fir_accum[4] <= 0;
        fir_accum[5] <= 0;
        fir_accum[6] <= 0;
        fir_accum[7] <= 0;
        fir_accum[8] <= 0;
        fir_accum[9] <= 0;
        fir_accum[10] <= 0;
        fir_accum[11] <= 0;
        fir_accum[12] <= 0;
        fir_accum[13] <= 0;
        fir_accum[14] <= 0;
        fir_accum[15] <= 0;
        fir_accum[16] <= 0;
        fir_accum[17] <= 0;
        fir_accum[18] <= 0;
        fir_accum[19] <= 0;
        fir_accum[20] <= 0;
        fir_accum[21] <= 0;
        fir_accum[22] <= 0;
        fir_accum[23] <= 0;
        fir_accum[24] <= 0;
        fir_accum[25] <= 0;
        fir_accum[26] <= 0;
        fir_accum[27] <= 0;
        fir_accum[28] <= 0;
    end else begin
        if(wren_one_clk_del) begin            
            fir_accum[0] <= fir_in_line[0] * fir_coeff[0];
            fir_accum[1] <= fir_in_line[1] * fir_coeff[1];
            fir_accum[2] <= fir_in_line[2] * fir_coeff[2];
            fir_accum[3] <= fir_in_line[3] * fir_coeff[3];
            fir_accum[4] <= fir_in_line[4] * fir_coeff[4];
            fir_accum[5] <= fir_in_line[5] * fir_coeff[5];
            fir_accum[6] <= fir_in_line[6] * fir_coeff[6];
            fir_accum[7] <= fir_in_line[7] * fir_coeff[7];
            fir_accum[8] <= fir_in_line[8] * fir_coeff[8];
            fir_accum[9] <= fir_in_line[9] * fir_coeff[9];
            fir_accum[10] <= fir_in_line[10] * fir_coeff[10];
            fir_accum[11] <= fir_in_line[11] * fir_coeff[11];
            fir_accum[12] <= fir_in_line[12] * fir_coeff[12];
            fir_accum[13] <= fir_in_line[13] * fir_coeff[13];
            fir_accum[14] <= fir_in_line[14] * fir_coeff[14];
            fir_accum[15] <= fir_in_line[15] * fir_coeff[15];
            fir_accum[16] <= fir_in_line[16] * fir_coeff[16];
            fir_accum[17] <= fir_in_line[17] * fir_coeff[17];
            fir_accum[18] <= fir_in_line[18] * fir_coeff[18];
            fir_accum[19] <= fir_in_line[19] * fir_coeff[19];
            fir_accum[20] <= fir_in_line[20] * fir_coeff[20];
            fir_accum[21] <= fir_in_line[21] * fir_coeff[21];
            fir_accum[22] <= fir_in_line[22] * fir_coeff[22];
            fir_accum[23] <= fir_in_line[23] * fir_coeff[23];
            fir_accum[24] <= fir_in_line[24] * fir_coeff[24];
            fir_accum[25] <= fir_in_line[25] * fir_coeff[25];
            fir_accum[26] <= fir_in_line[26] * fir_coeff[26];
            fir_accum[27] <= fir_in_line[27] * fir_coeff[27];
            fir_accum[28] <= fir_in_line[28] * fir_coeff[28];
        end else begin
            fir_accum[0] <= fir_accum[0];
            fir_accum[1] <= fir_accum[1];
            fir_accum[2] <= fir_accum[2];
            fir_accum[3] <= fir_accum[3];
            fir_accum[4] <= fir_accum[4];
            fir_accum[5] <= fir_accum[5];
            fir_accum[6] <= fir_accum[6];
            fir_accum[7] <= fir_accum[7];
            fir_accum[8] <= fir_accum[8];
            fir_accum[9] <= fir_accum[9];
            fir_accum[10] <= fir_accum[10];
            fir_accum[11] <= fir_accum[11];
            fir_accum[12] <= fir_accum[12];
            fir_accum[13] <= fir_accum[13];
            fir_accum[14] <= fir_accum[14];
            fir_accum[15] <= fir_accum[15];
            fir_accum[16] <= fir_accum[16];
            fir_accum[17] <= fir_accum[17];
            fir_accum[18] <= fir_accum[18];
            fir_accum[19] <= fir_accum[19];
            fir_accum[20] <= fir_accum[20];
            fir_accum[21] <= fir_accum[21];
            fir_accum[22] <= fir_accum[22];
            fir_accum[23] <= fir_accum[23];
            fir_accum[24] <= fir_accum[24];
            fir_accum[25] <= fir_accum[25];
            fir_accum[26] <= fir_accum[26];
            fir_accum[27] <= fir_accum[27];
            fir_accum[28] <= fir_accum[28];
        end
    end 
end
    // Create the adder logic
    always @(posedge s00_axi_aclk)
        if(!s00_axi_aresetn)
            fir_sum <= 0;
        else
            if(wren_one_clk_del) begin
                fir_sum <=  fir_accum[28] + fir_accum[27] + fir_accum[26] + 
                            fir_accum[25] + fir_accum[24] + fir_accum[23] + 
                            fir_accum[22] + fir_accum[21] + fir_accum[20] + 
                            fir_accum[19] + fir_accum[18] + fir_accum[17] + 
                            fir_accum[16] + fir_accum[15] + fir_accum[14] + 
                            fir_accum[13] + fir_accum[12] + fir_accum[11] + 
                            fir_accum[10] + fir_accum[9]  + fir_accum[8]  + 
                            fir_accum[8]  + fir_accum[7]  + fir_accum[6]  + 
                            fir_accum[5]  + fir_accum[4]  + fir_accum[3]  + 
                            fir_accum[2]  + fir_accum[1]  + fir_accum[0];
            end else
                fir_sum <= fir_sum;
	
    assign processed_data = fir_sum;
    
endmodule
