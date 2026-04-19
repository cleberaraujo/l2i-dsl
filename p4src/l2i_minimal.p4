// l2i_minimal.p4 — versão compatível com v1model (6 estágios)

#include <core.p4>
#include <v1model.p4>

// ---------------------------------------------------------------
// Cabeçalhos
// ---------------------------------------------------------------

header ethernet_t {
    bit<48> dst_addr;
    bit<48> src_addr;
    bit<16> eth_type;
}

header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

struct l2i_meta_t {
    bit<16> mcast_grp;
}

struct headers_t {
    ethernet_t ethernet;
    ipv4_t     ipv4;
}

struct metadata_t {
    l2i_meta_t l2i_meta;
}

// ---------------------------------------------------------------
// Parser
// ---------------------------------------------------------------

parser MyParser(
    packet_in packet,
    out headers_t hdr,
    inout metadata_t meta,
    inout standard_metadata_t stdmd
) {
    state start {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.eth_type) {
            0x0800: parse_ipv4;
            default: accept;
        }
    }

    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition accept;
    }
}

// ---------------------------------------------------------------
// Verify Checksum — obrigatório no V1Switch
// ---------------------------------------------------------------

control MyVerifyChecksum(
    inout headers_t hdr,
    inout metadata_t meta
) {
    apply { }
}

// ---------------------------------------------------------------
// Ingress
// ---------------------------------------------------------------

control MyIngress(
    inout headers_t hdr,
    inout metadata_t meta,
    inout standard_metadata_t stdmd
) {
    // Ações --------------------------------------------
    action set_dscp(bit<6> new_dscp) {
        if (hdr.ipv4.isValid()) {
            // sobrescreve os 6 bits mais altos do DSCP
            hdr.ipv4.diffserv[7:2] = new_dscp;
        }
    }

    action set_output_port(bit<9> port) {
        stdmd.egress_spec = port;
    }

    action set_mcast_group(bit<16> grp) {
        meta.l2i_meta.mcast_grp = grp;
    }

    // Tabelas ------------------------------------------
    table qos_table {
        key = { hdr.ipv4.dstAddr : lpm; }
        actions = { set_dscp; NoAction; }
        size = 1024;
        default_action = NoAction();
    }

    table unicast_table {
        key = { stdmd.ingress_port : exact; }
        actions = { set_output_port; NoAction; }
        size = 256;
        default_action = NoAction();
    }

    table mcast_table {
        key = { hdr.ipv4.dstAddr : lpm; }
        actions = { set_mcast_group; NoAction; }
        size = 256;
        default_action = NoAction();
    }

    // Pipeline -----------------------------------------
    apply {
        meta.l2i_meta.mcast_grp = 0;

        qos_table.apply();
        unicast_table.apply();
        mcast_table.apply();

        if (meta.l2i_meta.mcast_grp != 0) {
            stdmd.mcast_grp = meta.l2i_meta.mcast_grp;
        }
    }
}

// ---------------------------------------------------------------
// Compute Checksum — obrigatório no V1Switch
// ---------------------------------------------------------------

control MyComputeChecksum(
    inout headers_t hdr,
    inout metadata_t meta
) {
    apply {
        // Não recalculamos checksum no modelo mínimo.
    }
}

// ---------------------------------------------------------------
// Egress
// ---------------------------------------------------------------

control MyEgress(
    inout headers_t hdr,
    inout metadata_t meta,
    inout standard_metadata_t stdmd
) {
    apply { }
}

// ---------------------------------------------------------------
// Deparser
// ---------------------------------------------------------------

control MyDeparser(
    packet_out packet,
    in headers_t hdr
) {
    apply {
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);     // será emitido só se válido (comportamento oficial do P4)
    }
}

// ---------------------------------------------------------------
// Switch — assinatura correta do V1Switch
// ---------------------------------------------------------------

V1Switch(
    MyParser(),
         MyVerifyChecksum(),
         MyIngress(),
         MyEgress(),
         MyComputeChecksum(),
         MyDeparser()
) main;
