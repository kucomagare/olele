// File name: tcp_server_app.cpp
#include <iostream>
#include <cstring>
#include <cerrno>
#include <csignal>
#include <atomic>
#include <mutex>
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
// Global client sockets, guarded by fd_mutex
// ============================================================
std::mutex fd_mutex;
int pcb_fd    = -1;
int python_fd = -1;

std::atomic<bool> g_running{true};
int g_server_fd = -1;

void handle_signal(int)
{
    g_running = false;
    if (g_server_fd != -1) shutdown(g_server_fd, SHUT_RDWR);
    // Also unblock any worker thread stuck in a blocking send()/recv() on a
    // stalled peer, otherwise the join() loop in main() waits forever for it.
    if (pcb_fd != -1)    shutdown(pcb_fd, SHUT_RDWR);
    if (python_fd != -1) shutdown(python_fd, SHUT_RDWR);
}

bool send_all(int fd, const void *buf, size_t len)
{
    const uint8_t *p = static_cast<const uint8_t *>(buf);
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = send(fd, p + sent, len - sent, 0);
        if (n <= 0) return false;
        sent += static_cast<size_t>(n);
    }
    return true;
}

// ============================================================
// Client handler thread
// ============================================================
void handle_client(int client_fd, const std::string &ip, int port)
{
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

        std::lock_guard<std::mutex> lock(fd_mutex);

        if (client_fd == pcb_fd && python_fd != -1) {
            if (!send_all(python_fd, out_buf.data(), out_buf.size())) {
                std::cerr << "Forward PCB -> Python stalled/failed, dropping Python connection\n";
                shutdown(python_fd, SHUT_RDWR);
                close(python_fd);
                python_fd = -1;
            }
        }

        if (client_fd == python_fd && pcb_fd != -1) {
            if (!send_all(pcb_fd, out_buf.data(), out_buf.size())) {
                std::cerr << "Forward Python -> PCB stalled/failed, dropping PCB connection\n";
                shutdown(pcb_fd, SHUT_RDWR);
                close(pcb_fd);
                pcb_fd = -1;
            }
        }
    }

    std::cout << "Client disconnected: " << ip << ":" << port << std::endl;
    close(client_fd);

    std::lock_guard<std::mutex> lock(fd_mutex);
    if (client_fd == pcb_fd)    pcb_fd = -1;
    if (client_fd == python_fd) python_fd = -1;
}

// ============================================================
// Main server
// ============================================================
int main()
{
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

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

        {
            std::lock_guard<std::mutex> lock(fd_mutex);
            int &slot = is_pcb ? pcb_fd : python_fd;
            if (slot != -1) {
                std::cout << "Replacing existing " << (is_pcb ? "PCB" : "Python")
                          << " connection (closing old fd)\n";
                shutdown(slot, SHUT_RDWR);
                close(slot);
            }
            slot = client_fd;
        }

        std::cout << "Registered " << (is_pcb ? "PCB" : "Python") << " client\n";

        workers.emplace_back(handle_client, client_fd, std::string(ip_str), client_port);
    }

    close(server_fd);
    {
        std::lock_guard<std::mutex> lock(fd_mutex);
        if (pcb_fd != -1)    shutdown(pcb_fd, SHUT_RDWR);
        if (python_fd != -1) shutdown(python_fd, SHUT_RDWR);
    }
    for (auto &t : workers) if (t.joinable()) t.join();

    std::cout << "Server stopped." << std::endl;
    return 0;
}
