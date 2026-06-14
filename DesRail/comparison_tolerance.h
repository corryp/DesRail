#pragma once

#include <cmath>
#include <limits>

namespace comparison_tolerance {

    inline bool eq(double a, double b, double epsilon = std::numeric_limits<double>::epsilon()) {
        return std::abs(a - b) <= epsilon;
    }

    inline bool lt(double a, double b, double epsilon = std::numeric_limits<double>::epsilon()) {
        return !eq(a, b, epsilon) && a < b;
    }

    inline bool gt(double a, double b, double epsilon = std::numeric_limits<double>::epsilon()) {
        return !eq(a, b, epsilon) && a > b;
    }

    inline bool le(double a, double b, double epsilon = std::numeric_limits<double>::epsilon()) {
        return eq(a, b, epsilon) || a < b;
    }

    inline bool ge(double a, double b, double epsilon = std::numeric_limits<double>::epsilon()) {
        return eq(a, b, epsilon) || a > b;
    }

    inline double ssqrt(double val, double epsilon = std::numeric_limits<double>::epsilon()) {
        return val <= epsilon ? sqrt(0) : sqrt(val);
    }

} // comparison_tolerance
