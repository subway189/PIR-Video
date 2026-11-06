#include <iostream>
#include <string>
#include <fstream>
#include <cstdint>
#include <algorithm>
#include <climits>

#include <winsock2.h>
#include <ws2tcpip.h>

int PORT = 54000;
constexpr uint64_t MAX_CHUNK_SIZE = 64ULL * 1024 * 1024;

bool sendAll(SOCKET s, const char* data, size_t length) {
    size_t sent = 0;

    while (sent < length) {
        int n = send(s, data + sent,
                     static_cast<int>(std::min(length - sent,
                                               static_cast<size_t>(INT_MAX))),
                     0);
        if (n <= 0) return false;
        sent += static_cast<size_t>(n);
    }

    return true;
}

bool recvAll(SOCKET s, char* data, size_t length) {
    size_t received = 0;

    while (received < length) {
        int n = recv(s, data + received,
                     static_cast<int>(std::min(length - received,
                                               static_cast<size_t>(INT_MAX))),
                     0);
        if (n <= 0) return false;
        received += static_cast<size_t>(n);
    }

    return true;
}

int main(int argc, char* argv[]) {
    if ((argc != 4 && argc != 5) ||
        std::string(argv[1]) != "GET_CHUNK") {
        std::cerr << "Usage: ./client.exe GET_CHUNK sample CHUNK_ID [PORT]\n";
        return 1;
    }

    if (argc == 5) {
        try {
            size_t used = 0;
            PORT = std::stoi(argv[4], &used);

            if (used != std::string(argv[4]).size() ||
                PORT < 1024 || PORT > 65535) {
                throw std::invalid_argument("invalid port");
            }
        } catch (...) {
            std::cerr << "Invalid port\n";
            return 1;
        }
    }

    int chunkId;
    try {
        size_t used = 0;
        chunkId = std::stoi(argv[3], &used);

        if (used != std::string(argv[3]).size() || chunkId < 0) {
            throw std::invalid_argument("invalid chunk ID");
        }
    } catch (...) {
        std::cerr << "Chunk ID must be a non-negative integer\n";
        return 1;
    }

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::cerr << "WSAStartup failed\n";
        return 1;
    }

    SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == INVALID_SOCKET) {
        std::cerr << "Socket creation failed\n";
        WSACleanup();
        return 1;
    }

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(PORT);
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);

    if (connect(sock, reinterpret_cast<sockaddr*>(&address),
                sizeof(address)) == SOCKET_ERROR) {
        std::cerr << "Connection failed. Is the server running?\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    std::string request = "GET_CHUNK " + std::string(argv[2]) + " " +
                          std::to_string(chunkId) + "\n";

    if (!sendAll(sock, request.data(), request.size())) {
        std::cerr << "Failed to send request\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    char header[9];

    if (!recvAll(sock, header, sizeof(header))) {
        std::cerr << "Failed to receive response header\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    uint64_t size = 0;
    for (int i = 0; i < 8; ++i) {
        size = (size << 8) |
               static_cast<unsigned char>(header[i + 1]);
    }

    if (size > MAX_CHUNK_SIZE) {
        std::cerr << "Invalid response: chunk size exceeds 64 MiB. "
                  << "Check that the new server.exe is running.\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    std::string payload(static_cast<size_t>(size), '\0');

    if (size > 0 && !recvAll(sock, &payload[0], payload.size())) {
        std::cerr << "Incomplete response payload\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    closesocket(sock);
    WSACleanup();

    if (header[0] != '0') {
        std::cerr << "Server error: " << payload << '\n';
        return 1;
    }

    std::string outputPath = "downloaded_chunk.bin";
    std::ofstream output(outputPath, std::ios::binary);

    if (!output) {
        std::cerr << "Could not create output file\n";
        return 1;
    }

    output.write(payload.data(), static_cast<std::streamsize>(payload.size()));
    if (!output) {
        std::cerr << "Failed to write downloaded chunk\n";
        return 1;
    }

    std::cout << "Download successful: " << outputPath
              << " (" << size << " bytes)\n";
    return 0;
}
