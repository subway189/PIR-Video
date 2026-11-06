#include <iostream>
#include <sstream>
#include <string>
#include <fstream>
#include <iterator>
#include <cstdint>
#include <algorithm>

#include <winsock2.h>
#include <ws2tcpip.h>

#pragma comment(lib, "ws2_32.lib")

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

bool recvLine(SOCKET s, std::string& line) {
    line.clear();
    char ch;

    while (line.size() < 1024) {
        int n = recv(s, &ch, 1, 0);
        if (n <= 0) return false;
        if (ch == '\n') return true;
        if (ch != '\r') line += ch;
    }

    return false;
}

// Protocol: 1 status byte + 8-byte big-endian length + payload.
// Status '0' means success; status '1' means error.
bool sendResponse(SOCKET s, char status, const std::string& payload) {
    uint64_t size = payload.size();
    char header[9];
    header[0] = status;

    for (int i = 0; i < 8; ++i) {
        header[i + 1] =
            static_cast<char>((size >> (56 - 8 * i)) & 0xFF);
    }

    if (!sendAll(s, header, sizeof(header))) return false;

    if (!payload.empty()) {
        return sendAll(s, payload.data(), payload.size());
    }

    return true;
}

int main(int argc, char* argv[]) {
    if (argc > 1) {
        try {
            size_t used = 0;
            PORT = std::stoi(argv[1], &used);

            if (used != std::string(argv[1]).size() ||
                PORT < 1024 || PORT > 65535) {
                throw std::invalid_argument("invalid port");
            }
        } catch (...) {
            std::cerr << "Usage: ./server.exe [PORT]\n";
            return 1;
        }
    }
    
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::cerr << "WSAStartup failed\n";
        return 1;
    }

    SOCKET serverSocket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (serverSocket == INVALID_SOCKET) {
        std::cerr << "Socket creation failed\n";
        WSACleanup();
        return 1;
    }

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(PORT);
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);

    if (bind(serverSocket, reinterpret_cast<sockaddr*>(&address),
             sizeof(address)) == SOCKET_ERROR) {
        std::cerr << "Bind failed. Is another server using port "
                  << PORT << "?\n";
        closesocket(serverSocket);
        WSACleanup();
        return 1;
    }

    if (listen(serverSocket, SOMAXCONN) == SOCKET_ERROR) {
        std::cerr << "Listen failed\n";
        closesocket(serverSocket);
        WSACleanup();
        return 1;
    }

    std::cout << "Server listening on 127.0.0.1:" << PORT << "\n";

    while (true) {
        SOCKET client = accept(serverSocket, nullptr, nullptr);
        if (client == INVALID_SOCKET) {
            std::cerr << "Accept failed\n";
            continue;
        }

        std::string request;
        if (!recvLine(client, request)) {
            std::cerr << "Could not read request\n";
            closesocket(client);
            continue;
        }

        std::cout << "Request: " << request << '\n';

        std::istringstream input(request);
        std::string command, videoId, chunkText;
        input >> command;

        if (command == "PING") {
            sendResponse(client, '0', "PONG");
        } else if (command == "GET_CHUNK" &&
                   (input >> videoId >> chunkText)) {
            try {
                size_t used = 0;
                int chunkId = std::stoi(chunkText, &used);

                if (used != chunkText.size() ||
                    chunkId < 0 || videoId != "sample") {
                    sendResponse(client, '1', "Invalid video or chunk ID");
                } else {
                    std::string path =
                        "chunks/chunk_" + std::to_string(chunkId) + ".bin";

                    std::ifstream file(path, std::ios::binary);

                    if (!file) {
                        sendResponse(client, '1', "Chunk file not found");
                    } else {
                        std::string data(
                            (std::istreambuf_iterator<char>(file)),
                            std::istreambuf_iterator<char>());

                        if (data.size() > MAX_CHUNK_SIZE) {
                            sendResponse(client, '1', "Chunk exceeds size limit");
                        } else if (sendResponse(client, '0', data)) {
                            std::cout << "Sent " << data.size()
                                      << " bytes from " << path << '\n';
                        }
                    }
                }
            } catch (...) {
                sendResponse(client, '1', "Invalid chunk ID");
            }
        } else {
            sendResponse(client, '1', "Unknown or malformed request");
        }

        closesocket(client);
    }

    closesocket(serverSocket);
    WSACleanup();
    return 0;
}
