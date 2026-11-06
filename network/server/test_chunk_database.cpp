#include "ChunkDatabase.h"

#include <iostream>
#include <vector>

int main() {
    ChunkDatabase database;
    std::string error;

    if (!database.load("chunks", error)) {
        std::cerr << "Load failed: " << error << '\n';
        return 1;
    }

    std::cout << "Chunks loaded: " << database.size() << '\n';
    std::cout << "Fixed chunk size: "
              << database.chunkSize() << " bytes\n";

    for (std::size_t i = 0; i < database.size(); ++i) {
        std::vector<std::uint8_t> chunk;

        if (!database.getChunk(i, chunk)) {
            std::cerr << "Failed to retrieve chunk " << i << '\n';
            return 1;
        }

        std::cout << "Chunk " << i
                  << ": " << chunk.size() << " bytes\n";
    }

    std::vector<std::uint8_t> invalidChunk;

    if (database.getChunk(database.size(), invalidChunk)) {
        std::cerr << "Invalid index was unexpectedly accepted\n";
        return 1;
    }

    std::cout << "Invalid index correctly rejected\n";
    return 0;
}
