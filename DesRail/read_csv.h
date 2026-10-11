#pragma once

#include <iostream>
#include <fstream>
#include <sstream>
#include <vector>
#include <string>
#include <stdexcept>
#include <cctype>
#include <map>

using namespace std;

vector<vector<string>> read_csvf(const string& filename);

// ---------- Validation helpers for CSV-driven config ----------
//
// All helpers throw std::runtime_error with filename/row/column/value context
// when input is malformed. Callers typically let the exception propagate so
// the user sees a clear error rather than a segfault from invalid IDs.

namespace csv_check {

    inline string ctx(const string& file, size_t row, const string& label) {
        return file + " row " + to_string(row) + " (" + label + ")";
    }

    // Ensure csv[row] exists and has at least min_cols columns.
    inline void require_cols(const vector<vector<string>>& csv, size_t row, size_t min_cols,
                             const string& file) {
        if (row >= csv.size())
            throw runtime_error(file + " row " + to_string(row) + ": row out of bounds (csv has "
                                + to_string(csv.size()) + " rows)");
        if (csv[row].size() < min_cols)
            throw runtime_error(file + " row " + to_string(row) + ": expected at least "
                                + to_string(min_cols) + " columns, found "
                                + to_string(csv[row].size()));
    }

    // True for "", "0", "-12" etc. Lets a caller distinguish "this column holds an int I can
    // parse" from "this column holds something else entirely" (e.g. a file written to an older
    // column layout) and report the latter usefully, instead of failing inside parse_int.
    inline bool looks_like_int(const string& val) {
        size_t k = (!val.empty() && (val[0] == '+' || val[0] == '-')) ? 1 : 0;
        if (k >= val.size())
            return val.empty();
        for (size_t j = k; j < val.size(); ++j) {
            if (!isdigit(static_cast<unsigned char>(val[j])))
                return false;
        }
        return true;
    }

    inline int parse_int(const string& val, const string& file, size_t row, const string& label) {
        try { return stoi(val); }
        catch (const exception&) {
            throw runtime_error(ctx(file, row, label) + ": cannot parse '" + val + "' as int");
        }
    }

    inline double parse_double(const string& val, const string& file, size_t row, const string& label) {
        try { return stod(val); }
        catch (const exception&) {
            throw runtime_error(ctx(file, row, label) + ": cannot parse '" + val + "' as double");
        }
    }

    // Parse an int and require it is a valid 0-based index into a container of size 'upper'.
    inline int parse_id(const string& val, size_t upper,
                        const string& file, size_t row, const string& label) {
        int id = parse_int(val, file, row, label);
        if (id < 0 || static_cast<size_t>(id) >= upper)
            throw runtime_error(ctx(file, row, label) + ": ID " + to_string(id)
                                + " out of range [0, " + to_string(upper) + ")");
        return id;
    }

    // Lookup key in a map; throw if not found.
    template <typename V>
    V lookup(const map<string, V>& m, const string& key,
             const string& file, size_t row, const string& label) {
        auto it = m.find(key);
        if (it == m.end())
            throw runtime_error(ctx(file, row, label) + ": unknown key '" + key + "'");
        return it->second;
    }
}
