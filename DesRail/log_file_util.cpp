#include "log_file_util.h"

namespace lfu {
    
    Sim* sim;
    std::string out_folder;
    double derated_year_days;
    double warmup;
    double one_year;
    std::map<std::string, LogFile*> LogFile::master_files;

    void initialise_log_globals(
        Sim* _sim,
        double _warmup,
        string _out_folder,
        double _derated_year_days,
        double _one_year
    ) {
        sim = _sim;
        out_folder = _out_folder;
        derated_year_days = _derated_year_days;
        warmup = _warmup;
        one_year = _one_year;
    }

    void clear_master_logs() {
        /*for (auto& [key, ptr] : LogFile::master_files) {
            if (ptr != nullptr)
                delete ptr;
        }*/
        LogFile::master_files.clear();
    }

    void write(const std::string& a1, const std::string& a2, const std::string& a3, const std::string& a4, const std::string& a5, double v1, double v2, double v3, const std::string& comment) {
        static std::ofstream gf_summary;  // Make static to persist across calls
        if (!gf_summary.is_open()) {
            gf_summary.open(out_folder + "summary_output.csv");
            gf_summary << "a1,a2,a3,a4,a5,v1,v2,v3,comment\n";
        }
        gf_summary << a1 << "," << a2 << "," << a3 << "," << a4 << "," << a5 << "," << v1 << "," << v2 << "," << v3 << "," << comment << "\n";
    }

    void write(std::string& a1, const std::string& a2, const std::string& a3, double v1) {
        write(a1, a2, a3, "", "", v1, 0, 0, "");
    }

    double per_annum(double v) {
        return v * (derated_year_days / 365.0) / ((sim->max_time - warmup) / one_year);
    }

    double per_annum_progress(double v) {
        return v * (derated_year_days / 365.0) / ((sim->time_now - warmup) / one_year);
    }

    double utilisation(double time_used) {
        return time_used / (sim->max_time - warmup);
    }


    LogField::LogField(const std::string& _title, int _data_type, bool _write_log_switch, LogFile* master) :
        title(_title), 
        data_type(_data_type), 
        write_log_switch(_write_log_switch), 
        is_static(false), 
        dval(0.0), ival(0), str(""), tmp(0.0), tally(0.0), count(0), 
        global_tally(0.0), global_count(0) {

        master_field = master->field(_title);
        if (master_field == nullptr)
            master_field = this;
    }

    void LogField::data(string _str) { str = _str; }
    void LogField::data(double _dval) { dval = _dval; }
    void LogField::data(int _ival) { ival = _ival; }

    void LogField::start_delay() { tmp = sim->time_now; }
    void LogField::calc_delay() { dval += sim->time_now - tmp; }
    void LogField::timestamp() { dval = sim->time_now; }
    double LogField::avg() { return tally / std::max(count, 1); }
    double LogField::utilisation() { return lfu::utilisation(tally); }

    double LogField::global_avg() { return global_tally / global_count; }

    string LogField::write_str(LogFile* log) {
        std::string output_str;
        if (write_log_switch) {
            if (data_type == 1) {
                output_str = std::to_string(dval);
            }
            else if (data_type == 2) {
                output_str = std::to_string(ival);
            }
            else {
                output_str = str;
            }
        }

        if (sim->time_now >= warmup) {
            tally += dval + ival;
            ++count;
            master_field->global_tally += dval + ival;
            ++master_field->global_count;
        }

        if (!is_static) {
            dval = 0.0;
            ival = 0;
            str = "";
        }

        return output_str;
    }

    LogFile::LogFile(const std::string& _name, bool _write_log_switch, bool _auto_timestamp, bool _write_to_screen) :
        name(_name), 
        write_log_switch(_write_log_switch),
        auto_timestamp(_auto_timestamp),
        write_to_screen(_write_to_screen),
        master(nullptr), 
        f(nullptr) 
    {
        auto it = master_files.find(name);
        if (it == master_files.end()) {
            master_files[name] = this;
            master = this;
        }
        else
            master = it->second;
        if (auto_timestamp)
            add("time", "double");
    }

    LogFile::~LogFile()
    {
        for (int i = 0; i < fields.size(); i++)
            delete fields[i];
        fields.clear();
    }

    LogField* LogFile::field(const std::string& title) {

        auto it = rtr_fields.find(title);
        if (it != rtr_fields.end()) {
            f = it->second;
        }
        else {
            f = nullptr; // Or throw an exception if the field is not found.
        }
        return f;
    }

    void lfu::LogFile::data(string field_id, string val) { field(field_id)->data(val); }
    void lfu::LogFile::data(string field_id, double val) { field(field_id)->data(val); }
    void lfu::LogFile::data(string field_id, int val) { field(field_id)->data(val); }

    void LogFile::write_current_record() {
        if (write_log_switch) {
            if (!master->logf.is_open()) {
                init_file();
            }

            if (auto_timestamp)
                fields[0]->timestamp();

            bool first = true;
            string record;
            for (LogField* field : fields) {
                if (!first) {
                    record += ",";
                }

                record += field->write_str(this);
                first = false;
            }
            master->logf << record << "\n";
            if (write_to_screen)
                cout << record << endl;
        }//if
    }

    void LogFile::add(const std::string title, const std::string data_type) {
        LogField* field;

        if (data_type == "double") {
            field = new LogField(title, 1, write_log_switch, master);
        }
        else if (data_type == "int") {
            field = new LogField(title, 2, write_log_switch, master);
        }
        else {
            field = new LogField(title, 3, write_log_switch, master);
        }
        fields.push_back(field);
        rtr_fields[title] = field; // Add to retrieval map
    }

    void LogFile::add_static_field(const std::string title, const std::string str) {
        add(title, "string");
        fields.back()->is_static = true;
        fields.back()->str = str;
    }

    void LogFile::init_file() {
        master->logf.open(out_folder + name + ".csv");
        bool first = true;
        string header;
        for (LogField* field : fields) {
            if (first) {
                header += field->title;
            }
            else {
                header += "," + field->title;
            }
            first = false;
        }
        master->logf << header << "\n";
        if (write_to_screen)
            cout << header << endl;
    }
 
}//lfu
