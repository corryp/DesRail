#include <string>
#include <random>

#include "simulator.h"
#include "log_file_util.h"
#include "desrail.h"
#include "dr_anim.h"
#include "rail_sim_tools.h"
#include "read_csv.h"

using namespace DesRail;
using namespace lfu;

void run_desrail_single_replicate() {
	RailSimMaster master("./input/", "./output/");

	LogFile* summary_log = master.run();

	int train_count = summary_log->field("total_wait_time")->global_count;
	cout << train_count << " trains in summary statistics data set." << endl;

	cout << "initialisation time: " << master.sim_init_time << " seconds." << endl;
	cout << "simulation run time: " << master.sim_run_time << " seconds." << endl;
}//rail_tools_test

void cpu_time_experiment(int t_min, int t_max, int n_reps, int seed) {
	LogFile results("cpu_exp.csv", true, false, true);
	results.add("Tmax", "int");
	results.add("rep", "int");
	results.add("run_t", "double");
	results.add("train_ctr", "int");
	
	std::mt19937 rng;
	rng.seed(seed);
	std::uniform_int_distribution<> dist(t_min, t_max);

	for (int r = 0; r < n_reps; r++) {
		RailSimMaster master("./input/", "./output/");
		int t = dist(rng);
		master.sim.set_max_time(t);
		master.random_seed = seed + r;
		LogFile* summary_log = master.run();

		int train_count = summary_log->field("total_wait_time")->global_count;
		results.data("Tmax", t);
		results.data("rep", r);
		double run_t = master.sim_init_time + master.sim_run_time;
		results.data("run_t", run_t);
		results.data("train_ctr", train_count);
		results.write_current_record();
		//cout << "repitition " << r << ": " << run_t << "s" << endl;
	}//r
}

void throughput_experiment(double ml_arv_rate, double wl_arv_rate, int n_reps, int seed) {
	LogFile results("thput_exp.csv", true, false, true);
	results.add("rep", "int");
	results.add("run_t", "double");
	results.add("avg_speed", "double");
	results.add("train_ctr", "int");

	for (int r = 0; r < n_reps; r++) {
		RailSimMaster master("./input/", "./output/");
		master.random_seed = seed + r;

		for (auto ols : master.open_loop_spawners) {
			if (ols->name == "ols1" || ols->name == "ols2" || ols->name == "ols3" || ols->name == "ols4")
				ols->dist_prm1 = ml_arv_rate;
			else
				ols->dist_prm1 = wl_arv_rate;
		}//ols

		LogFile* summary_log = master.run();

		int train_count = summary_log->field("total_wait_time")->global_count;
		double avg_speed = summary_log->field("avg_speed")->global_avg();
		results.data("rep", r);
		double run_t = master.sim_init_time + master.sim_run_time;
		results.data("run_t", run_t);
		results.data("train_ctr", train_count);
		results.data("avg_speed", avg_speed);
		results.write_current_record();

		//cout << "repetition " << r << ": " << run_t << "s, " << train_count << " trains" << endl;
	}//r
}

int main(int argc, char* argv[]) {
	vector<vector<string>> csv = read_csvf("./input/main_option.csv");

	if (csv[1][1] == "single_replicate")
		run_desrail_single_replicate();
	else if (csv[1][1] == "cpu_time_exp") {
		int tmin = stoi(argv[1]);
		int tmax = stoi(argv[2]);
		int nreps = stoi(argv[3]);
		int seed = stoi(argv[4]);
		cpu_time_experiment(tmin, tmax, nreps, seed);
	}
	else if (csv[1][1] == "throughput_exp") {
		double ml_arv_rate = stod(argv[1]);
		double wl_arv_rate = stod(argv[2]);
		int nreps = stoi(argv[3]);
		int seed = stoi(argv[4]);
		throughput_experiment(ml_arv_rate, wl_arv_rate, nreps, seed);
	}
	else
		cout << "Unknown main option: " << csv[1][1] << endl;
}//main