`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 04/08/2026 10:05:20 PM
// Design Name: 
// Module Name: fpga_top
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


module fpga_top(
    input clk,
    input btn_pl,
    output led_pl_b,
    output led_pl_g,
    output led_pl_r
    );
    
reg [23:0] count = 0;
wire pwm;

assign pwm = count[4:0] == 5'b11111; 
assign led_pl_g =  btn_pl    & pwm;
assign led_pl_b =  count[23] & pwm;
assign led_pl_r = ~count[23] & pwm;
 
always @ (posedge(clk)) count <= count + 1;
endmodule
