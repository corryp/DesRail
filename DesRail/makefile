# Compiler
CXX = g++
# Common flags
WARNINGS = -Wall -Wextra
STD = -std=c++20

# Directories
SRC_DIRS = . desviz_src
SRCS = $(foreach dir,$(SRC_DIRS),$(wildcard $(dir)/*.cpp))
OBJS = $(SRCS:.cpp=.o)
DEPS = $(SRCS:.cpp=.d)

# Executable name
TARGET = desrail

# Default build mode (Release)
CXXFLAGS = $(STD) $(WARNINGS) -O3
LDFLAGS =

# Debug mode
debug: CXXFLAGS = $(STD) $(WARNINGS) -g -O0 -fno-omit-frame-pointer
debug: LDFLAGS = 
debug: all

# Release mode
release: CXXFLAGS = $(STD) $(WARNINGS) -O3
release: all

# Default target
all: $(TARGET)

# Link
$(TARGET): $(OBJS)
	$(CXX) $(CXXFLAGS) -o $@ $(OBJS) $(LDFLAGS)

# Compile source files into object files and generate dependencies
%.o: %.cpp
	$(CXX) $(CXXFLAGS) -MMD -MP -c $< -o $@

# Include dependency files
-include $(DEPS)

# Clean up build files
clean:
	rm -f $(OBJS) $(DEPS) $(TARGET)

# Run the program
run: $(TARGET)
	./$(TARGET)
