#pragma once

#include <iostream>
#include <fstream>
#include <string>
#include <vector>
#include <iomanip> // For setting precision
#include <map>
#include "simulator.h"

namespace lfu {
    class LogField;
    class LogFile;

    extern Sim* sim;
    extern string out_folder;
    extern double derated_year_days;
    extern double warmup;
    extern double warmdown;   // stats window END; 0 = unset (fall back to sim->max_time)
    extern double one_year;

    // Effective end of the stats-collection window: start_warmdown if set,
    // otherwise the simulation horizon. Denominator for rate/utilisation calcs.
    double stats_window_end();

    //extern double time; // TODO: will need to pass the time in where needed

    void initialise_log_globals(
        Sim* _sim,
        double _warmup = 0,
        string _out_folder = "./output/",
        double _derated_year_days = 365,
        double _one_year = 8760
        );

    void write(const std::string& a1, const std::string& a2, const std::string& a3, double v1);
    void write(const std::string& a1, const std::string& a2, const std::string& a3, const std::string& a4, const std::string& a5, double v1, double v2, double v3, const std::string& comment);
    double per_annum(double v);
    double per_annum_progress (double v);
    double utilisation(double time_used);
    void clear_master_logs();

    class LogFile;

    class LogField {
    public:
        LogField(const std::string& _title, int _data_type, bool _write_log_switch, LogFile* master);

        void data(string _str); //update field data with supplied value
        void data(double _dval);
        void data(int _ival);
        void start_delay();     //call this to timestamp the start of a delay calculation
        void calc_delay();      //call this to update field data with the calculated delay
        void timestamp();       //call this to update field data with a time stamp
        double avg();           //call this to update field data with an average
        double utilisation();   //call this to update field data with utilisation
        int count;              //count of number of entries for this instance of the log field
        int global_count;       //master count of number of entries for all instances of the log field
        double global_tally;    //master tally of all instances
        double global_avg();

        double dval;            //variable if field is "double"
        int ival;               //variable if field is "int"
    
        
    private:
        string write_str(LogFile* log);

        std::string title;
        int data_type; // 1 = double, 2 = int, 3 = string
        bool write_log_switch;
        bool is_static;     //true will not clear data values after each write to the log
        std::string str;
        double tmp;
        double tally;
        LogField* master_field;
        friend class LogFile;
    };

    class LogFile {
    public:
        LogFile(const std::string&c_name, bool _write_log_switch, bool _auto_timestamp= true, bool _write_to_screen= false);
        ~LogFile();

        LogField* field(const std::string& title);
        void data(string field_id, string val);
        void data(string field_id, double val);
        void data(string field_id, int val);
        void write_current_record();
        void add(const std::string title, const std::string data_type);
        void add_static_field(const std::string title, const std::string str);

        std::string name;
        bool write_log_switch;
        bool write_to_screen;
        bool auto_timestamp;
        std::vector<LogField*> fields;
        std::ofstream logf;
        LogFile* master;

        static std::map<std::string, LogFile*> master_files;
    private:
        void init_file();
        std::map<std::string, LogField*> rtr_fields; // For quick retrieval
        LogField* f; // Most recently used field_

    };

} // namespace lfu
