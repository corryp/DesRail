#include "read_csv.h"

vector<vector<string>> read_csvf(const string& filename) {
    vector<vector<string>> data;
    ifstream file(filename);

    if (!file.is_open()) {
        cerr << "Could not open file: " << filename << endl;
        return data;
    }

    string line;
    while (getline(file, line)) {
        // Strip a trailing CR so CRLF-terminated files (the Windows-authored
        // inputs) parse identically on Linux: otherwise the last field of every
        // row keeps a '\r', which silently breaks string comparisons such as the
        // 'END' terminators in train_charts.csv.
        if (!line.empty() && line.back() == '\r')
            line.pop_back();

        vector<string> row;
        stringstream ss(line);
        string cell;

        while (getline(ss, cell, ',')) {
            row.push_back(cell);
        }

        data.push_back(row);
    }

    file.close();
    return data;
}