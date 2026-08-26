// File name: tcp_server_app.cpp
#include <iostream>
#include <cstring>
#include <cerrno>
#include <csignal>
#include <atomic>
#include <mutex>
#include <memory>
#include <string>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>   // TCP_NODELAY
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <vector>
#include <thread>
#include "packet_format.h"

constexpr int SERVER_PORT = 5001;
constexpr const char *PCB_IP = "192.168.1.10";
constexpr int MAX_SAMPLES = 2000;     // must match MAX_PAYLOAD_SAMPLES in lwip_comm_client_raw.c
                                       // (bounds record count, not bytes -- record size is per-type,
                                       // see packet_format.h)
constexpr int RECV_TIMEOUT_SEC = 30;  // reap peers that go silent without closing the TCP connection
constexpr int SEND_TIMEOUT_SEC = 2;   // cap how long a forward can block on a stalled peer

// ============================================================
// Peer registry
//
// fd_mutex guards ONLY the two registry pointers (below, after send_all) --
// never a send. Holding one lock across a blocking send() was the original
// design and it meant a peer that was slow to drain locked out the *other*
// direction too, for up to SEND_TIMEOUT_SEC: the board's thread, stuck
// writing to a busy Python client, held the same mutex the Python thread
// needed to forward to the board, so one slow reader stalled both ways and
// made the innocent side look guilty. Each Peer now carries its own send
// mutex, so the two directions are independent.
// ============================================================
std::mutex fd_mutex;

// Raw fd mirrors, for handle_signal() only. A signal handler must not take a
// mutex or touch a shared_ptr, so it gets plain atomics to shutdown().
std::atomic<int> g_pcb_raw_fd{-1};
std::atomic<int> g_python_raw_fd{-1};

std::atomic<bool> g_running{true};
int g_server_fd = -1;

void handle_signal(int)
{
    g_running = false;
    if (g_server_fd != -1) shutdown(g_server_fd, SHUT_RDWR);
    // Also unblock any worker thread stuck in a blocking send()/recv() on a
    // stalled peer, otherwise the join() loop in main() waits forever for it.
    int fd;
    if ((fd = g_pcb_raw_fd.load())    != -1) shutdown(fd, SHUT_RDWR);
    if ((fd = g_python_raw_fd.load()) != -1) shutdown(fd, SHUT_RDWR);
}

bool send_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = static_cast<const uint8_t *>(buf);
    size_t sent = 0;
    while (sent < len) {
        // MSG_NOSIGNAL, not 0: without it, writing to a socket whose peer
        // has already gone raises SIGPIPE, and SIGPIPE's default action is
        // to kill the process. That is not hypothetical here -- at high
        // packet rates this relay is inside send() almost continuously, so
        // restarting the Python client killed the relay outright, mid-line,
        // with nothing in the log. With the signal suppressed the call just
        // fails with EPIPE and the caller's existing "dropping connection"
        // path handles it, which is what was always intended.
        ssize_t n = send(fd, p + sent, len - sent, MSG_NOSIGNAL);
        if (n <= 0) return false;
        sent += static_cast<size_t>(n);
    }
    return true;
}

// ============================================================
// Peer: one connected client, owning its fd
//
// The fd is closed by ~Peer, i.e. only once the last shared_ptr to it is
// gone. That is what makes it safe to send outside fd_mutex: a sender holds
// a reference for the duration of the call, so the number it is writing to
// cannot be closed and handed to an unrelated socket underneath it. Dropping
// a peer calls kill(), which shutdown()s -- unblocking anyone already inside
// send()/recv() and failing every later call -- without closing.
// ============================================================
struct Peer {
    int fd;
    std::mutex send_mtx;             // serializes writes to THIS fd only
    std::atomic<bool> dead{false};

    explicit Peer(int f) : fd(f) {}
    ~Peer() { if (fd != -1) ::close(fd); }
    Peer(const Peer &) = delete;
    Peer &operator=(const Peer &) = delete;

    bool send(const void *buf, size_t len)
    {
        if (dead.load()) return false;
        std::lock_guard<std::mutex> lock(send_mtx);
        if (dead.load()) return false;   // re-check: kill() may have landed
                                         // while we waited for send_mtx
        return send_all(fd, buf, len);
    }

    void kill()
    {
        if (!dead.exchange(true) && fd != -1) ::shutdown(fd, SHUT_RDWR);
    }
};

std::shared_ptr<Peer> pcb_peer;      // both guarded by fd_mutex
std::shared_ptr<Peer> python_peer;

// Unregister `p` from whichever slot still holds it. Safe to call twice.
void drop_peer(const std::shared_ptr<Peer> &p)
{
    std::lock_guard<std::mutex> lock(fd_mutex);
    if (pcb_peer == p)    { pcb_peer.reset();    g_pcb_raw_fd    = -1; }
    if (python_peer == p) { python_peer.reset(); g_python_raw_fd = -1; }
}

// ============================================================
// Client handler thread
// ============================================================
void handle_client(std::shared_ptr<Peer> self, const std::string &ip, int port, bool is_pcb)
{
    const int client_fd = self->fd;
    int packet_counter = 0;

    timeval tv{RECV_TIMEOUT_SEC, 0};
    setsockopt(client_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    timeval send_tv{SEND_TIMEOUT_SEC, 0};
    setsockopt(client_fd, SOL_SOCKET, SO_SNDTIMEO, &send_tv, sizeof(send_tv));

    // Relaying is latency-critical: this process sits in the middle of a
    // request/response path, and every packet ends in a partial segment.
    // With Nagle on, that tail waits for an ACK before going out, which
    // serialized the whole pipeline to one packet per round trip.
    int nodelay = 1;
    setsockopt(client_fd, IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));

    while (g_running)
    {
        uint16_t type, length;

        ssize_t r = recv(client_fd, &type, sizeof(type), MSG_WAITALL);
        if (r <= 0) {
            if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) continue;
            break;
        }

        r = recv(client_fd, &length, sizeof(length), MSG_WAITALL);
        if (r <= 0) {
            if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) continue;
            break;
        }

        type   = ntohs(type);
        length = ntohs(length);

        uint32_t record_size = packet_record_size(type);
        if (record_size == 0) {
            std::cerr << "[" << ip << ":" << port << "] unknown packet type "
                      << type << ", dropping connection\n";
            break;
        }

        if (length > MAX_SAMPLES) {
            std::cerr << "[" << ip << ":" << port << "] packet exceeds MAX_SAMPLES ("
                      << length << " > " << MAX_SAMPLES << "), dropping connection\n";
            break;
        }

        // Kept as raw wire bytes (network/big-endian order) and forwarded verbatim below —
        // both the board and Python already agree on big-endian, so there is nothing to decode.
        std::vector<uint8_t> raw(static_cast<size_t>(length) * record_size);
        if (!raw.empty()) {
            ssize_t rr = recv(client_fd, raw.data(), raw.size(), MSG_WAITALL);
            if (rr <= 0) {
                if (rr < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) continue;
                break;
            }
        }

        packet_counter++;
        if (packet_counter % 500 == 0) {
            std::cout << "[" << ip << ":" << port << "] "
                      << "Processed " << packet_counter
                      << " packets (type=" << type
                      << ", len=" << length << ")" << std::endl;
        }

        uint16_t type_n   = htons(type);
        uint16_t length_n = htons(length);

        // Assembled once and sent as a single write per hop, instead of three
        // separate small sends, so Nagle/delayed-ACK can't stall the header
        // writes waiting on an ACK for the previous segment.
        std::vector<uint8_t> out_buf(4 + raw.size());
        memcpy(out_buf.data(), &type_n, sizeof(type_n));
        memcpy(out_buf.data() + 2, &length_n, sizeof(length_n));
        if (!raw.empty())
            memcpy(out_buf.data() + 4, raw.data(), raw.size());

        // Snapshot the partner (and confirm we are still the registered peer
        // for our own side -- a replaced connection must stop relaying) while
        // holding fd_mutex, then RELEASE it before sending. The send itself
        // serializes on that peer's own send_mtx, so the two directions no
        // longer block each other.
        std::shared_ptr<Peer> partner;
        {
            std::lock_guard<std::mutex> lock(fd_mutex);
            if (self == pcb_peer)         partner = python_peer;
            else if (self == python_peer) partner = pcb_peer;
            else break;   // we were replaced; stop relaying
        }

        if (ip == "127.0.0.1") {
            // Loopback self-test: echo straight back to the sender instead
            // of routing through a PCB/Python partner. This is what
            // BOARD_CONNECTED=False actually needs -- the wire path (this
            // relay's framing/TCP_NODELAY handling) still round-trips, it
            // just doesn't require a second peer, since there's no board
            // to be one.
            if (!self->send(out_buf.data(), out_buf.size())) {
                std::cerr << "Loopback echo failed for " << ip << ":" << port
                          << ", dropping connection\n";
                break;
            }
            continue;
        }

        if (partner) {
            if (!partner->send(out_buf.data(), out_buf.size())) {
                // Message text unchanged: it is what the logs have always
                // said and what any grep over them expects.
                std::cerr << (is_pcb ? "Forward PCB -> Python stalled/failed, dropping Python connection\n"
                                     : "Forward Python -> PCB stalled/failed, dropping PCB connection\n");
                partner->kill();
                drop_peer(partner);
            }
        }
    }

    std::cout << "Client disconnected: " << ip << ":" << port << std::endl;
    // No close() here: ~Peer does it when the last reference drops, which may
    // be a forwarding thread still inside send() on this fd.
    self->kill();
    drop_peer(self);
}

// ============================================================
// Main server
// ============================================================
int main()
{
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);
    // Belt-and-braces alongside MSG_NOSIGNAL in send_all(): that flag covers
    // the sends this program actually makes, this covers anything that ever
    // writes to a dead peer without it. A dead peer is a connection to drop,
    // never a reason to take the whole relay down.
    std::signal(SIGPIPE, SIG_IGN);

    int server_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server_fd < 0) {
        perror("socket failed");
        return 1;
    }
    g_server_fd = server_fd;

    int opt = 1;
    setsockopt(server_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(SERVER_PORT);
    addr.sin_addr.s_addr = INADDR_ANY;

    if (bind(server_fd, (sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind failed");
        return 1;
    }

    if (listen(server_fd, 5) < 0) {
        perror("listen failed");
        return 1;
    }

    std::cout << "Server waiting for connection on port " << SERVER_PORT << "..." << std::endl;

    std::vector<std::thread> workers;

    while (g_running)
    {
        sockaddr_in client_addr{};
        socklen_t client_len = sizeof(client_addr);

        int client_fd = accept(server_fd, (sockaddr *)&client_addr, &client_len);
        if (client_fd < 0) {
            if (!g_running) break;
            perror("accept failed");
            continue;
        }

        char ip_str[INET_ADDRSTRLEN];
        inet_ntop(AF_INET, &client_addr.sin_addr, ip_str, sizeof(ip_str));
        int client_port = ntohs(client_addr.sin_port);

        std::cout << "Client connected from " << ip_str << ":" << client_port << std::endl;

        bool is_pcb = (strcmp(ip_str, PCB_IP) == 0);

        auto peer = std::make_shared<Peer>(client_fd);
        {
            std::lock_guard<std::mutex> lock(fd_mutex);
            auto &slot = is_pcb ? pcb_peer : python_peer;
            if (slot) {
                std::cout << "Replacing existing " << (is_pcb ? "PCB" : "Python")
                          << " connection (closing old fd)\n";
                slot->kill();   // shutdown only; its own thread closes it
            }
            slot = peer;
            (is_pcb ? g_pcb_raw_fd : g_python_raw_fd) = client_fd;
        }

        std::cout << "Registered " << (is_pcb ? "PCB" : "Python") << " client\n";

        workers.emplace_back(handle_client, peer, std::string(ip_str), client_port, is_pcb);
    }

    close(server_fd);
    {
        std::lock_guard<std::mutex> lock(fd_mutex);
        if (pcb_peer)    pcb_peer->kill();
        if (python_peer) python_peer->kill();
    }
    for (auto &t : workers) if (t.joinable()) t.join();

    std::cout << "Server stopped." << std::endl;
    return 0;
}
