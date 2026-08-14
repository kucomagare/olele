#!/usr/bin/env python3
# Generates a C header from packet_format.json -- the single source of
# truth for wire packet/sample structure shared by the firmware
# (vitis/sizif/app/lwip_comm_client_raw.c) and the C++ relay
# (pc_app/sizif/tcp_server_app.cpp). Both are compiled, bare-metal C has
# no filesystem to read JSON from at runtime, so their view of the format
# has to be baked in at build time instead -- run this before compiling.
#
# python_client.py doesn't need this: it's interpreted, so it just loads
# packet_format.json directly at runtime.
#
# Usage: gen_packet_header.py <packet_format.json> <output.h>
import json
import sys

BITS_TO_CTYPE = {
    (8, False):  "uint8_t",
    (8, True):   "int8_t",
    (16, False): "uint16_t",
    (16, True):  "int16_t",
    (32, False): "uint32_t",
    (32, True):  "int32_t",
}


def c_type(field):
    key = (field["bits"], field["signed"])
    if key not in BITS_TO_CTYPE:
        raise ValueError(f"Unsupported field width/signedness: {field}")
    return BITS_TO_CTYPE[key]


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <packet_format.json> <output.h>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        config = json.load(f)

    packet_types = config["packet_types"]
    lines = []

    lines.append("/* AUTO-GENERATED from packet_format.json by gen_packet_header.py --")
    lines.append("   do not edit by hand, edit packet_format.json and rebuild instead. */")
    lines.append("#ifndef PACKET_FORMAT_H")
    lines.append("#define PACKET_FORMAT_H")
    lines.append("")
    lines.append("#include <stdint.h>")
    lines.append("")

    for type_id, ptype in packet_types.items():
        lines.append(f'#define PACKET_TYPE_{ptype["name"].upper()} {type_id}')
    lines.append("")

    lines.append("#pragma pack(push, 1)")
    for type_id, ptype in packet_types.items():
        lines.append(f'typedef struct {{')
        for field in ptype["fields"]:
            lines.append(f'    {c_type(field)} {field["name"]};')
        lines.append(f'}} packet_{ptype["name"]}_t;')
    lines.append("#pragma pack(pop)")
    lines.append("")

    lines.append("/* Byte width of each field, in wire order, per packet type -- used to")
    lines.append("   endian-swap raw wire bytes into/out of the structs above without")
    lines.append("   needing per-field code when bit widths change in packet_format.json. */")
    for type_id, ptype in packet_types.items():
        name_upper = ptype["name"].upper()
        field_bytes = ", ".join(str(f["bits"] // 8) for f in ptype["fields"])
        lines.append(
            f'static const uint8_t PACKET_{name_upper}_FIELD_BYTES[] '
            f'__attribute__((unused)) = {{{field_bytes}}};'
        )
    lines.append("")

    lines.append("static inline uint32_t packet_record_size(uint16_t type)")
    lines.append("{")
    lines.append("    switch (type) {")
    for type_id, ptype in packet_types.items():
        lines.append(f'    case PACKET_TYPE_{ptype["name"].upper()}: return sizeof(packet_{ptype["name"]}_t);')
    lines.append("    default: return 0;")
    lines.append("    }")
    lines.append("}")
    lines.append("")

    lines.append("static inline const uint8_t *packet_field_bytes(uint16_t type, uint32_t *n_fields)")
    lines.append("{")
    lines.append("    switch (type) {")
    for type_id, ptype in packet_types.items():
        name_upper = ptype["name"].upper()
        lines.append(f'    case PACKET_TYPE_{name_upper}:')
        lines.append(f'        *n_fields = sizeof(PACKET_{name_upper}_FIELD_BYTES);')
        lines.append(f'        return PACKET_{name_upper}_FIELD_BYTES;')
    lines.append("    default:")
    lines.append("        *n_fields = 0;")
    lines.append("        return NULL;")
    lines.append("    }")
    lines.append("}")
    lines.append("")
    lines.append("#endif /* PACKET_FORMAT_H */")

    with open(sys.argv[2], "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
