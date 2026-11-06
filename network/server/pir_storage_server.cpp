#include "ChunkDatabase.h"

#include <winsock2.h>
#include <ws2tcpip.h>

#include <cstdint>
#include <iostream>
#include <string>
#include <vector>

#pragma comment(lib, "Ws2_32.lib")

constexpr int DEFAULT_PORT = 54002;
constexpr std::size_t MAX_REQUEST_SIZE = 128;

bool recvLine(SOCKET socket, std::string& line) {
    line.clear();

    char ch;
    while (line.size() < MAX_REQUEST_SIZE) {
        int received = recv(socket, &ch, 1, 0);

        if (received <= 0) {
            return false;
        }

        if (ch == '\n') {
            return true;
        }

        line.push_back(ch);
    }

    return false;
}

bool sendAll(SOCKET socket, const char* data, std::size_t size) {
    while (size > 0) {
        int sent = send(
            socket, data,
            static_cast<int>(size > 1'000'000 ? 1'000'000 : size),
            0
        );

        if (sent <= 0) {
            return false;
        }

        data += sent;
        size -= static_cast<std::size_t>(sent);
    }

    return true;
}

bool sendResponse(
    SOCKET socket,
    bool success,
    const std::vector<std::uint8_t>& payload
) {
    // Response format: status byte + 8-byte big-endian length + payload.
    char header[9];
    header[0] = success ? '0' : '1';

    std::uint64_t length = success ? payload.size() : 0;

    for (int i = 8; i >= 1; --i) {
        header[i] = static_cast<char>(length & 0xFF);
        length >>= 8;
    }

    if (!sendAll(socket, header, sizeof(header))) {
        return false;
    }

    return !success ||
           sendAll(socket,
                   reinterpret_cast<const char*>(payload.data()),
                   payload.size());
}

int main(int argc, char* argv[]) {
    int port = DEFAULT_PORT;

    if (argc >= 2) {
        try {
            port = std::stoi(argv[1]);
        } catch (...) {
            std::cerr << "Usage: ./pir_storage_server.exe [PORT]\n";
            return 1;
        }

        if (port < 1024 || port > 65535) {
            std::cerr << "Port must be between 1024 and 65535.\n";
            return 1;
        }
    }

    ChunkDatabase database;
    std::string error;

    if (!database.load("chunks", error)) {
        std::cerr << "Database load failed: " << error << '\n';
        return 1;
    }

    std::cout << "Loaded " << database.size()
              << " chunks, " << database.chunkSize()
              << " bytes per chunk.\n";

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::cerr << "WSAStartup failed.\n";
        return 1;
    }

    SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listener == INVALID_SOCKET) {
        std::cerr << "Socket creation failed.\n";
        WSACleanup();
        return 1;
    }

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<u_short>(port));
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);

    if (bind(listener, reinterpret_cast<sockaddr*>(&address),
             sizeof(address)) == SOCKET_ERROR ||
        listen(listener, SOMAXCONN) == SOCKET_ERROR) {
        std::cerr << "Bind/listen failed on port " << port << ".\n";
        closesocket(listener);
        WSACleanup();
        return 1;
    }

    std::cout << "PIR storage server listening on 127.0.0.1:"
              << port << '\n';
    std::cout << "Request format: GET_CHUNK GLOBAL_INDEX\\n\n";

    while (true) {
        SOCKET client = accept(listener, nullptr, nullptr);

        if (client == INVALID_SOCKET) {
            std::cerr << "Accept failed.\n";
            continue;
        }

        std::string request;
        bool success = recvLine(client, request);
        std::vector<std::uint8_t> payload;

        if (success) {
            const std::string prefix = "GET_CHUNK ";

            if (request.compare(0, prefix.size(), prefix) != 0) {
                success = false;
            } else {
                const std::string number = request.substr(prefix.size());

                try {
                    std::size_t used = 0;
                    unsigned long long index =
                        std::stoull(number, &used);

                    if (used != number.size() ||
                        number.empty() || number[0] == '-' ||
                        index >= database.size()) {
                        success = false;
                    } else {
                        success = database.getChunk(
                            static_cast<std::size_t>(index), payload
                        );
                    }
                } catch (...) {
                    success = false;
                }
            }
        }

        if (!sendResponse(client, success, payload)) {
            std::cerr << "Failed to send response.\n";
        }

        closesocket(client);
    }

    // The server runs indefinitely; these are retained for completeness.
    closesocket(listener);
    WSACleanup();
    return 0;
}
