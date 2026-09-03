`timescale 1ns / 1ps
// LED PWM blink test module -- board bring-up sanity check, not part of the signal path.

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
