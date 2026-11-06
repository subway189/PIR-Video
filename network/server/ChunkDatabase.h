#ifndef CHUNK_DATABASE_H
#define CHUNK_DATABASE_H

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

class ChunkDatabase {
public:
    bool load(const std::string& directory, std::string& error);

    std::size_t size() const;
    std::size_t chunkSize() const;

    bool getChunk(
        std::size_t index,
        std::vector<std::uint8_t>& output
    ) const;

private:
    std::vector<std::vector<std::uint8_t>> chunks_;
    std::size_t chunkSize_ = 0;
};

#endif
