#include <winsock2.h>
#include <ws2tcpip.h>

#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

bool recvAll(SOCKET socket, char* data, std::size_t size) {
    while (size > 0) {
        int received = recv(
            socket, data,
            static_cast<int>(size > 1'000'000 ? 1'000'000 : size),
            0
        );

        if (received <= 0) {
            return false;
        }

        data += received;
        size -= static_cast<std::size_t>(received);
    }

    return true;
}

int main(int argc, char* argv[]) {
    if (argc < 2 || argc > 3) {
        std::cerr << "Usage: ./test_pir_storage_client.exe CHUNK_INDEX [PORT]\n";
        return 1;
    }

    std::size_t index;
    int port = 54002;

    try {
        std::string arg = argv[1];
        std::size_t used = 0;
        unsigned long long parsed = std::stoull(arg, &used);

        if (arg.empty() || arg[0] == '-' || used != arg.size()) {
            throw std::invalid_argument("invalid index");
        }

        index = static_cast<std::size_t>(parsed);

        if (argc == 3) {
            port = std::stoi(argv[2]);
        }

        if (port < 1024 || port > 65535) {
            throw std::invalid_argument("invalid port");
        }
    } catch (...) {
        std::cerr << "Invalid chunk index or port.\n";
        return 1;
    }

    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::cerr << "WSAStartup failed.\n";
        return 1;
    }

    SOCKET sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == INVALID_SOCKET) {
        std::cerr << "Socket creation failed.\n";
        WSACleanup();
        return 1;
    }

    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(static_cast<u_short>(port));
    inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);

    if (connect(sock, reinterpret_cast<sockaddr*>(&address),
                sizeof(address)) == SOCKET_ERROR) {
        std::cerr << "Connection failed. Is the server running?\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    std::string request = "GET_CHUNK " + std::to_string(index) + "\n";

    int sent = send(sock, request.c_str(),
                    static_cast<int>(request.size()), 0);

    if (sent != static_cast<int>(request.size())) {
        std::cerr << "Failed to send request.\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    char header[9];

    if (!recvAll(sock, header, sizeof(header))) {
        std::cerr << "Failed to receive response header.\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    std::uint64_t length = 0;

    for (int i = 1; i < 9; ++i) {
        length = (length << 8) |
                 static_cast<unsigned char>(header[i]);
    }

    if (header[0] != '0') {
        std::cerr << "Server rejected the request.\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    if (length > 64ULL * 1024 * 1024) {
        std::cerr << "Response exceeds the allowed size.\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    std::vector<char> data(static_cast<std::size_t>(length));

    if (!data.empty() &&
        !recvAll(sock, data.data(), data.size())) {
        std::cerr << "Incomplete chunk received.\n";
        closesocket(sock);
        WSACleanup();
        return 1;
    }

    closesocket(sock);
    WSACleanup();

    const std::string output =
        "pir_downloaded_chunk_" + std::to_string(index) + ".bin";

    std::ofstream file(output, std::ios::binary);

    if (!file ||
        (!data.empty() &&
         !file.write(data.data(), static_cast<std::streamsize>(data.size())))) {
        std::cerr << "Failed to save output file.\n";
        return 1;
    }

    std::cout << "Chunk index: " << index << '\n';
    std::cout << "Bytes received: " << length << '\n';
    std::cout << "Saved to: " << output << '\n';

    return 0;
}
