#include "ChunkDatabase.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <sstream>
#include <utility>
#include <exception>

namespace fs = std::filesystem;

bool ChunkDatabase::load(
    const std::string& directory,
    std::string& error
) {
    chunks_.clear();
    chunkSize_ = 0;
    error.clear();

    try {
        if (!fs::exists(directory) || !fs::is_directory(directory)) {
            error = "Chunk directory does not exist: " + directory;
            return false;
        }

        std::map<std::size_t, fs::path> files;

        for (const auto& entry : fs::directory_iterator(directory)) {
            if (!entry.is_regular_file()) {
                continue;
            }

            const std::string name =
                entry.path().filename().string();

            const std::string prefix = "chunk_";
            const std::string suffix = ".bin";

            if (name.size() <= prefix.size() + suffix.size()) {
                continue;
            }

            if (name.compare(0, prefix.size(), prefix) != 0 ||
                name.compare(name.size() - suffix.size(),
                             suffix.size(), suffix) != 0) {
                continue;
            }

            const std::string digits = name.substr(
                prefix.size(),
                name.size() - prefix.size() - suffix.size()
            );

            if (digits.empty() ||
                !std::all_of(digits.begin(), digits.end(),
                    [](unsigned char c) {
                        return c >= '0' && c <= '9';
                    })) {
                continue;
            }

            std::size_t index = 0;
            std::istringstream parser(digits);
            parser >> index;

            if (!parser || !parser.eof()) {
                error = "Invalid chunk index in filename: " + name;
                return false;
            }

            if (!files.emplace(index, entry.path()).second) {
                error = "Duplicate chunk index: " + digits;
                return false;
            }
        }

        if (files.empty()) {
            error = "No chunk_N.bin files found in: " + directory;
            return false;
        }

        std::size_t expectedIndex = 0;

        for (const auto& [index, path] : files) {
            if (index != expectedIndex) {
                error = "Missing chunk_" +
                        std::to_string(expectedIndex) + ".bin";
                return false;
            }
            ++expectedIndex;
        }

        std::vector<std::vector<std::uint8_t>> loaded;
        loaded.reserve(files.size());

        for (const auto& [index, path] : files) {
            const auto fileSize = fs::file_size(path);

            if (fileSize == 0) {
                error = "Empty chunk file: " + path.string();
                return false;
            }

            if (fileSize > std::numeric_limits<std::size_t>::max()) {
                error = "Chunk is too large: " + path.string();
                return false;
            }

            const std::size_t bytes =
                static_cast<std::size_t>(fileSize);

            std::vector<std::uint8_t> data(bytes);

            std::ifstream input(path, std::ios::binary);
            if (!input) {
                error = "Cannot open chunk file: " + path.string();
                return false;
            }

            input.read(
                reinterpret_cast<char*>(data.data()),
                static_cast<std::streamsize>(data.size())
            );

            if (!input || static_cast<std::size_t>(input.gcount()) != bytes) {
                error = "Failed to read complete chunk: " + path.string();
                return false;
            }

            chunkSize_ = std::max(chunkSize_, bytes);
            loaded.push_back(std::move(data));
        }

        // Pad every chunk to the same record size.
        for (auto& chunk : loaded) {
            chunk.resize(chunkSize_, 0);
        }

        chunks_ = std::move(loaded);
        return true;
    }
    catch (const std::exception& e) {
        chunks_.clear();
        chunkSize_ = 0;
        error = e.what();
        return false;
    }
}

std::size_t ChunkDatabase::size() const {
    return chunks_.size();
}

std::size_t ChunkDatabase::chunkSize() const {
    return chunkSize_;
}

bool ChunkDatabase::getChunk(
    std::size_t index,
    std::vector<std::uint8_t>& output
) const {
    if (index >= chunks_.size()) {
        return false;
    }

    output = chunks_[index];
    return true;
}
