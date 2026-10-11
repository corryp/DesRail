#include "rail_sim_tools.h"
#include <string>
#include <map>
#include <random>
#include <cassert>
#include <functional>
#include <queue>
#include <set>
#include <chrono>
#include <algorithm>
#include "read_csv.h"
#include "desrail.h"
#include "comparison_tolerance.h"
#include "desviz_src/desviz_script.h"

using namespace std;
using namespace DesRail;
using namespace comparison_tolerance;

//RailSimMaster implementation==============================

int OpenLoopTrainSpawner::ols_spawn_ctr = 0;
int ClosedLoopTrainSpawner::cls_spawn_ctr = 0;

RailSimMaster::RailSimMaster(string _input_dir, string _output_dir, int seed, RailNetwork* _network, bool _delete_network) :
	sim(0), input_dir(_input_dir), output_dir(_output_dir), network(_network), delete_network(_delete_network),
	animate(false), random_seed(seed), anim_rng(1), pl_constraints(true), pl_constraint_mode(0) {

	OpenLoopTrainSpawner::ols_spawn_ctr = 0;

	auto start = chrono::high_resolution_clock::now();
	initialise();
	auto end = chrono::high_resolution_clock::now();

	chrono::duration<double> elapsed = end - start;
	sim_init_time = elapsed.count();
}

RailSimMaster::~RailSimMaster()
{
	if (delete_network)
		delete network;
	for (auto* ols : open_loop_spawners)
		delete ols;
	open_loop_spawners.clear();
	for (auto* cls : closed_loop_spawners)
		delete cls;
	closed_loop_spawners.clear();
	for (auto* js : job_spawners)
		delete js;
	job_spawners.clear();
	delete manager;
	manager = nullptr;
	for (auto* plg : pl_groups)
		delete plg;
	pl_groups.clear();
	for (auto& [key, ptr] : rs_templates) {
		delete ptr;
	}
	rs_templates.clear();
	for (auto& [key, ptr] : train_templates) {
		delete ptr;
	}
	train_templates.clear();
}

LogFile* RailSimMaster::run()
{
	auto start = chrono::high_resolution_clock::now();

	rng.seed(random_seed);
	if (manager != nullptr)
		manager->activate();
	for (auto ols : open_loop_spawners)
		ols->activate();
	for (auto cls : closed_loop_spawners)
		cls->activate();
	for (auto js : job_spawners)
		js->activate();

	sim.run();

	TrainChartTrace::flush_active();		// final horizon sample per active train, so deadlock flatlines are drawn

	auto end = chrono::high_resolution_clock::now();
	chrono::duration<double> elapsed = end - start;
	sim_run_time = elapsed.count();

	if (animate)
		script->write();

	return DesRail::mlog_master;
}

void RailSimMaster::reset()
{
	// Destroy old spawners + manager (SimObjects on this sim)
	for (auto* ols : open_loop_spawners)
		delete ols;
	open_loop_spawners.clear();
	OpenLoopTrainSpawner::ols_spawn_ctr = 0;
	for (auto* cls : closed_loop_spawners)
		delete cls;
	closed_loop_spawners.clear();
	ClosedLoopTrainSpawner::cls_spawn_ctr = 0;
	for (auto* js : job_spawners)
		delete js;
	job_spawners.clear();
	delete manager;
	manager = nullptr;

	// Reset simulation engine (clears events, conditions, object lists, time)
	sim.reset();

	// Reset network (clears segment/section/arc state, recreates signals_mgr on this sim)
	network->reset(sim);

	// Recreate manager + spawners from cached config (no file IO). The manager is
	// recreated before the closed-loop spawners so the fleet wires to it at spawn.
	manager = new TrainManagerBase(sim, network);
	create_open_loop_spawners();
	create_persistent_train_spawners();
	create_job_spawners();

	// Re-apply runctrl settings (sim max_time, logging, etc.)
	apply_runctrl();
}

void RailSimMaster::set_animation(string scriptf,
	double _anim_start_t, double _anim_end_t, bool _render_cars, bool _render_sprites_to_scale) 
{
	animate = true;
	anim_start_t = _anim_start_t;
	anim_end_t = _anim_end_t;
	render_sprites_to_scale = _render_sprites_to_scale;
	render_cars = _render_cars;
	script = new AnimScript(scriptf, sim);

	for (auto arc : network->arcs)
		script->set_path_true_length(arc->str_id, arc->segment->length);
}

void RailSimMaster::initialise()
{
	const bool time_init = env_is_set("DESRAIL_TIME_INIT");
	auto mark = chrono::steady_clock::now();
	auto lap = [&](const char* what) {
		if (!time_init) return;
		auto now = chrono::steady_clock::now();
		cout << "[init] " << what << ": " << chrono::duration<double>(now - mark).count() << " s\n";
		mark = now;
	};
	if (!network) {
		network = read_network(input_dir + "network.csv");
		lap("read_network");
		read_signals(network, input_dir + "signals.csv");
		read_signal_constraints(network, input_dir + "signal_constraints.csv");
		read_speed_limits(network, input_dir + "speed_limits.csv");
		read_violations(network, input_dir + "violations.csv");
		read_violation_triplets(network, input_dir + "violation_triplets.csv");
		lap("reads to violation_triplets");
		read_priorities(network, input_dir + "priorities.csv");
		read_parameters(network, input_dir + "general_params.csv");
		network->initialise_network();
		lap("initialise_network");

		// Pre-read pl_constraints flag before passing loops are configured
		vector<vector<string>> runctrl_pre = read_csvf(input_dir + "runctrl.csv");
		for (int i = 1; i < runctrl_pre.size(); ++i) {
			if (runctrl_pre[i][0] == "pl_constraints")
				pl_constraints = stoi(runctrl_pre[i][1]) != 0;
			else if (runctrl_pre[i][0] == "pl_constraint_mode")
				pl_constraint_mode = stoi(runctrl_pre[i][1]);
		}

		read_passing_loops(input_dir + "passing_loops.csv");
		lap("read_passing_loops");

		network->process_network(sim);
		lap("process_network");
	}
	else {
		network->reset(sim);
	}

	read_rs_templates(input_dir + "rs_templates.csv");
	read_train_templates(input_dir + "train_templates.csv");
	read_open_loop_spawners(input_dir + "open_loop_spawners.csv");

	// Closed-loop dispatch: build the manager (needed by persistent trains) then
	// read the (optional) persistent-train fleets and job sources. Missing files
	// yield empty caches -> no closed-loop objects, so existing open-loop-only
	// datasets are unaffected.
	manager = new TrainManagerBase(sim, network);
	read_persistent_trains(input_dir + "persistent_trains.csv");
	read_job_spawners(input_dir + "job_spawner.csv", input_dir + "job_spawner_task.csv");

	read_runctrl(input_dir + "runctrl.csv");
	lap("rolling stock, spawners, runctrl");

	if (rst_debug)
		write_sections(network, output_dir + "sections.csv");
	lap("write_sections (debug output)");
}

void RailSimMaster::read_passing_loops(string fname)
{
	PassingLoopGroup* plg = nullptr;
	vector<vector<string>> csv = read_csvf(fname);
	int prev_id = -1;
	for (int i = 1; i < csv.size(); i++) {
		csv_check::require_cols(csv, i, 6, fname);
		int id = csv_check::parse_int(csv[i][0], fname, i, "pl_group_id");
		if (id != prev_id) {
			plg = new PassingLoopGroup(id);
			pl_groups.push_back(plg);
		}
		prev_id = id;
		int seg = csv_check::parse_id(csv[i][1], network->segments.size(), fname, i, "segment_id");
		int hd = csv_check::parse_id(csv[i][2], network->nodes.size(), fname, i, "head_node_id");
		double margin = csv_check::parse_double(csv[i][3], fname, i, "margin");
		double opp_speed = csv_check::parse_double(csv[i][4], fname, i, "opp_speed");
		DirectedSegment* arc = network->segments[seg]->get_arc(hd);
		if (!arc)
			throw runtime_error(csv_check::ctx(fname, i, "arc lookup")
				+ ": segment " + to_string(seg) + " has no arc with head node " + to_string(hd));
		// max_length is column 6 when present (8+ columns: ..., arc_type, max_length, comment)
		// Without max_length column, there are 7 columns (..., arc_type, comment)
		double max_length = (csv[i].size() > 7 && !csv[i][6].empty())
			? csv_check::parse_double(csv[i][6], fname, i, "max_length") : 0.0;
		if (csv[i][5] == "APPR0")
			plg->add_appr(0, arc, margin, opp_speed);
		else if (csv[i][5] == "APPR1")
			plg->add_appr(1, arc, margin, opp_speed);
		else if (csv[i][5] == "ML0")
			plg->add_mainline(0, arc, margin, opp_speed, max_length);
		else if (csv[i][5] == "ML1")
			plg->add_mainline(1, arc, margin, opp_speed, max_length);
		else if (csv[i][5] == "S0")
			plg->add_siding(0, arc, margin, opp_speed, max_length);
		else if (csv[i][5] == "S1")
			plg->add_siding(1, arc, margin, opp_speed, max_length);
	}
	for (auto p : pl_groups)
		p->configure(network, pl_constraints, pl_constraint_mode);
}//read_passing_loops

void RailSimMaster::read_rs_templates(string fname) {
	vector<vector<string>> rs_csv = read_csvf(fname);

	for (int i = 1; i < rs_csv.size(); ++i) {
		csv_check::require_cols(rs_csv, i, 6, fname);
		int r = rs_csv[i].size() > 6 ? csv_check::parse_int(rs_csv[i][6], fname, i, "color_r") : 255;
		int g = rs_csv[i].size() > 7 ? csv_check::parse_int(rs_csv[i][7], fname, i, "color_g") : 255;
		int b = rs_csv[i].size() > 8 ? csv_check::parse_int(rs_csv[i][8], fname, i, "color_b") : 255;
		rs_templates[rs_csv[i][0]] = new RollingStockTemplate(
			rs_csv[i][0],
			csv_check::parse_double(rs_csv[i][1], fname, i, "length"),
			rs_csv[i][2],
			csv_check::parse_double(rs_csv[i][3], fname, i, "mass"),
			csv_check::parse_double(rs_csv[i][4], fname, i, "resistance_a"),
			csv_check::parse_double(rs_csv[i][5], fname, i, "resistance_b"),
			r, g, b);
	}
}//read_rs_templates

void RailSimMaster::read_train_templates(string fname) {
	vector<vector<string>> tt_csv = read_csvf(fname);

	for (int i = 1; i < tt_csv.size(); ++i) {
		csv_check::require_cols(tt_csv, i, 4, fname);
		train_templates[tt_csv[i][0]] = new TrainTemplate(
			tt_csv[i][0],
			csv_check::parse_double(tt_csv[i][1], fname, i, "max_accel"),
			csv_check::parse_double(tt_csv[i][2], fname, i, "max_decel"),
			csv_check::parse_double(tt_csv[i][3], fname, i, "max_speed"));
		TrainTemplate& tt = *(train_templates[tt_csv[i][0]]);
		for (int col = 4; col < tt_csv[i].size(); col += 2) {
			if (tt_csv[i][col] == "")
				break;
			if (col + 1 >= (int)tt_csv[i].size())
				throw runtime_error(csv_check::ctx(fname, i, "rs count at col " + to_string(col))
					+ ": missing rolling stock name in next column");
			int n = csv_check::parse_int(tt_csv[i][col], fname, i, "rolling_stock_count");
			RollingStockTemplate* rst = csv_check::lookup(rs_templates, tt_csv[i][col + 1], fname, i, "rolling_stock_name");
			for (int j = 0; j < n; ++j)
				tt.append_rolling_stock(rst);
		}//k
	}//i
}//read_train_templates

void RailSimMaster::read_open_loop_spawners(string fname) {
	cached_ols_csv = read_csvf(fname);
	create_open_loop_spawners();
}

void RailSimMaster::create_open_loop_spawners() {
	const string fname = "open_loop_spawners.csv";  // logical name for error context
	if (cached_ols_csv.empty())
		return;

	// Two supported header layouts:
	//   legacy: name, spawn_seg, spawn_head, train_template, distribution, dist_prm1, dist_prm2, terminal1, dwell1, ...
	//   new:    service_name, <spawn_terminal>, train_template, distribution, dist_prm1, dist_prm2, terminal1, dwell1, ...
	// Detect by anchoring on train_template — it sits at col 2 in the new
	// (single spawn col) format and col 3 in the legacy (two spawn cols).
	// Anything that isn't the new-format anchor is treated as legacy.
	const auto& header = cached_ols_csv[0];
	bool legacy = !(header.size() >= 3 && header[2] == "train_template");
	int spawn_cols = legacy ? 2 : 1;
	int base_task_start = 1 + spawn_cols + 4;  // service_name + spawn cols + tt/distr/p1/p2
	// Optional "stop_time" column immediately after dist_prm2, before the task pairs.
	bool has_stop_col = (int)header.size() > base_task_start && header[base_task_start] == "stop_time";
	int stop_col = has_stop_col ? base_task_start : -1;
	int task_start = base_task_start + (has_stop_col ? 1 : 0);
	// Task columns come in repeating groups. Historically (terminal, dwell); a file
	// that books departures adds a third, (terminal, dwell, earliest). Detected by
	// HEADER NAME, like stop_time above, so the two layouts coexist and no existing
	// open_loop_spawners.csv has to be rewritten to keep working.
	int task_stride = ((int)header.size() > task_start + 2
		&& header[task_start + 2].rfind("earliest", 0) == 0) ? 3 : 2;

	for (int i = 1; i < cached_ols_csv.size(); ++i) {
		csv_check::require_cols(cached_ols_csv, i, task_start, fname);
		string name = cached_ols_csv[i][0];

		int spawn_seg, spawn_head;
		if (legacy) {
			spawn_seg = csv_check::parse_id(cached_ols_csv[i][1], network->segments.size(), fname, i, "spawn_segment");
			spawn_head = csv_check::parse_id(cached_ols_csv[i][2], network->nodes.size(), fname, i, "spawn_head_node");
		}
		else {
			string spawn_term = cached_ols_csv[i][1];
			Terminal* term = csv_check::lookup(network->terminal_map, spawn_term, fname, i, "spawn_terminal");
			if (term->arcs.empty())
				throw runtime_error(csv_check::ctx(fname, i, "spawn_terminal")
					+ ": terminal '" + spawn_term + "' has no arcs to spawn on");
			DirectedSegment* arc = term->arcs[0];
			spawn_seg = arc->segment->id;
			spawn_head = arc->head->id;
		}

		int c = 1 + spawn_cols;
		string tt = cached_ols_csv[i][c];
		string distr = cached_ols_csv[i][c + 1];
		double prm1 = csv_check::parse_double(cached_ols_csv[i][c + 2], fname, i, "dist_prm1");
		double prm2 = csv_check::parse_double(cached_ols_csv[i][c + 3], fname, i, "dist_prm2");

		TrainTemplate* ttp = csv_check::lookup(train_templates, tt, fname, i, "train_template");
		open_loop_spawners.push_back(new OpenLoopTrainSpawner(name, this, spawn_seg, spawn_head, ttp, distr, prm1, prm2));
		OpenLoopTrainSpawner& ols = *(open_loop_spawners.back());
		if (stop_col >= 0) {
			const string& sv = cached_ols_csv[i][stop_col];
			if (!sv.empty() && sv != "NA")
				ols.stop_time = csv_check::parse_double(sv, fname, i, "stop_time");
		}
		for (int j = task_start; j < cached_ols_csv[i].size(); j += task_stride) {
			string terminal = cached_ols_csv[i][j];
			if (terminal != "NA" && terminal != "") {
				if (j + 1 >= (int)cached_ols_csv[i].size())
					throw runtime_error(csv_check::ctx(fname, i, "terminal at col " + to_string(j))
						+ ": terminal '" + terminal + "' has no dwell value in next column");
				double dwell = csv_check::parse_double(cached_ols_csv[i][j + 1], fname, i, "dwell");
				bool stop = (j >= (int)cached_ols_csv[i].size() - task_stride) || gt(dwell, 0);
				// Booked departure, when the layout carries one. Blank or NA means this
				// call is not booked and the task keeps its plain minimum-dwell behaviour.
				double earliest = -1.0;
				if (task_stride == 3 && j + 2 < (int)cached_ols_csv[i].size()) {
					const string& ev = cached_ols_csv[i][j + 2];
					if (!ev.empty() && ev != "NA")
						earliest = csv_check::parse_double(ev, fname, i, "earliest_departure");
				}
				// Validate terminal exists before add_task (which would otherwise
				// insert a nullptr into terminal_map via operator[]).
				csv_check::lookup(network->terminal_map, terminal, fname, i, "terminal");
				ols.add_task(terminal, stop, dwell, earliest);
			}
		}//j
	}//i
}

void RailSimMaster::read_runctrl(string fname)
{
	cached_runctrl_csv = read_csvf(fname);
	apply_runctrl();
}

void RailSimMaster::apply_runctrl()
{
	double sim_len = 0;

	// Reset the stats-window end each apply; set below iff start_warmdown given.
	start_warmdown = 0;
	lfu::warmdown = 0;

	// Pre-scan for render_cars so it's available when animate is parsed
	render_cars = false;
	for (int i = 1; i < cached_runctrl_csv.size(); ++i) {
		if (cached_runctrl_csv[i][0] == "render_cars")
			render_cars = stoi(cached_runctrl_csv[i][1]) > 0;
	}

	for (int i = 1; i < cached_runctrl_csv.size(); ++i) {
		string label = cached_runctrl_csv[i][0];
		if (label == "sim_len") {
			sim_len = stod(cached_runctrl_csv[i][1]);
			sim.set_max_time(sim_len);
		}
		else if (label == "warmup") {
			//warmup period for stats collection
			double warmup = stod(cached_runctrl_csv[i][1]);
			lfu::initialise_log_globals(&sim, warmup);
		}
		else if (label == "start_warmdown") {
			// End of the stats-collection window. Also the default spawn-stop
			// time for spawners without an explicit stop_time, and (when < sim_len)
			// enables drain semantics in the DeadlockMonitor. Denominator for
			// rate/utilisation calcs becomes (start_warmdown - warmup).
			start_warmdown = stod(cached_runctrl_csv[i][1]);
			lfu::warmdown = start_warmdown;
		}
		else if (label == "screen_output") {
			//screen output verbosity
			Train::write_log_to_screen = false;
			Train::write_metric_to_screen = false;
			int verbosity = stoi(cached_runctrl_csv[i][1]);
			if (verbosity == 2)
				Train::write_log_to_screen = true;
			else if (verbosity == 1)
				Train::write_metric_to_screen = true;
		}
		else if (label == "animate") {
			//animation flag
			int anim_flag = stoi(cached_runctrl_csv[i][1]);
			if (anim_flag > 0)
				set_animation("../Animation/anim_script.json", 0, sim_len, render_cars, render_cars);
		}
		else if (label == "render_cars") {
			render_cars = stoi(cached_runctrl_csv[i][1]) > 0;
		}
		else if (label == "log_output") {
			//log file output verbosity
			Train::write_train_log = false;
			Train::write_metric_log = true;
			int verbosity = stoi(cached_runctrl_csv[i][1]);
			if (verbosity == 2)
				Train::write_train_log = true;
			else if (verbosity == 0)
				Train::write_metric_log = false;
		}
		else if (label == "section_log") {
			// Independent of log_output on purpose: the section log is the cheap output a
			// calibration or capacity run wants, and it should compose with either verbosity
			// rather than being a third point on a single verbosity scale.
			Train::write_section_log = stoi(cached_runctrl_csv[i][1]) > 0;
		}
		else if (label == "seed")
			random_seed = stoi(cached_runctrl_csv[i][1]);
		else if (label == "train_charts") {
			if (cached_runctrl_csv[i][1] != "0")
				read_train_charts(input_dir + "train_charts.csv");
		}
		else if (label == "pl_constraints" || label == "pl_constraint_mode" || label == "deadlock_check_interval" || label == "deadlock_timeout") {
			// handled elsewhere (pl_constraints/pl_constraint_mode pre-read in initialise, deadlock params in deadlock_avoidance_exp)
		}
		else
			network->record_warning(nullptr, nullptr, string("read_runctrl: unrecognised run control label - ") + label);
	}//i
}

void RailSimMaster::read_train_charts(string fname)
{
	// LONG/NORMALIZED format: one row per element, grouped by chart_id (col 0):
	//   chart_id, kind, f1, f2, f3
	//     kind=name  -> f1 = name (cosmetic here; the chart id is the corridor id)
	//     kind=start -> f1 = segment, f2 = head, f3 = base_chg (optional, default 0)
	//     kind=exit  -> f1 = node
	//     kind=entry -> f1 = segment, f2 = head
	// A chart is the set of rows sharing chart_id. Multiple start rows = multiple
	// chainage datums (parallel entry tracks each get an origin).
	vector<vector<string>> csv = read_csvf(fname);

	map<int, TrainChart*> by_id;
	for (int i = 1; i < csv.size(); ++i) {
		if (csv[i].size() < 2 || csv[i][0].empty() || csv[i][1].empty())
			continue;
		int cid = stoi(csv[i][0]);
		string kind = csv[i][1];
		TrainChart* tc;
		auto it = by_id.find(cid);
		if (it == by_id.end()) {
			tc = new TrainChart(cid, network);
			by_id[cid] = tc;
			train_charts.push_back(tc);
		} else
			tc = it->second;

		if (kind == "name") {
			// cosmetic; the log uses the numeric chart id as corridor_id
		} else if (kind == "start") {
			csv_check::require_cols(csv, i, 4, fname);
			int seg = csv_check::parse_id(csv[i][2], network->segments.size(), fname, i, "start_segment");
			int head = csv_check::parse_id(csv[i][3], network->nodes.size(), fname, i, "start_head_node");
			double base = (csv[i].size() > 4 && !csv[i][4].empty()) ? stod(csv[i][4]) : 0.0;
			tc->add_start(seg, head, base);
		} else if (kind == "exit") {
			csv_check::require_cols(csv, i, 3, fname);
			int node = csv_check::parse_id(csv[i][2], network->nodes.size(), fname, i, "exit_node");
			tc->add_exit_node(node);
		} else if (kind == "entry") {
			csv_check::require_cols(csv, i, 4, fname);
			int seg = csv_check::parse_id(csv[i][2], network->segments.size(), fname, i, "entry_segment");
			int head = csv_check::parse_id(csv[i][3], network->nodes.size(), fname, i, "entry_head_node");
			tc->add_entry_arc(seg, head);
		} else {
			throw runtime_error(csv_check::ctx(fname, i, "kind")
				+ ": unknown train-chart row kind '" + kind + "'");
		}
	}//i

	for (auto& kv : by_id) {
		if (kv.second->starts.empty())
			throw runtime_error(fname + ": train chart " + to_string(kv.first)
				+ " has no 'start' row (needs at least one chainage datum)");
		kv.second->initialise();
	}
}


//Closed-loop dispatch: persistent trains + job spawners========

void RailSimMaster::read_persistent_trains(string fname) {
	cached_pt_csv = read_csvf(fname);
	create_persistent_train_spawners();
}

void RailSimMaster::create_persistent_train_spawners() {
	const string fname = "persistent_trains.csv";	// logical name for error context
	if (cached_pt_csv.empty())
		return;
	// header: train_group, train_template_name, spawn_terminal, spawn_interval, spawn_count
	for (int i = 1; i < (int)cached_pt_csv.size(); ++i) {
		csv_check::require_cols(cached_pt_csv, i, 5, fname);
		string train_group = cached_pt_csv[i][0];
		TrainTemplate* ttp = csv_check::lookup(train_templates, cached_pt_csv[i][1], fname, i, "train_template_name");
		Terminal* term = csv_check::lookup(network->terminal_map, cached_pt_csv[i][2], fname, i, "spawn_terminal");
		double interval = csv_check::parse_double(cached_pt_csv[i][3], fname, i, "spawn_interval");
		int count = csv_check::parse_int(cached_pt_csv[i][4], fname, i, "spawn_count");
		closed_loop_spawners.push_back(
			new ClosedLoopTrainSpawner(train_group, this, ttp, term, interval, count));
	}//i
}

void RailSimMaster::read_job_spawners(string fname, string task_fname) {
	cached_js_csv = read_csvf(fname);
	cached_jst_csv = read_csvf(task_fname);
	create_job_spawners();
}

void RailSimMaster::create_job_spawners() {
	const string pf = "job_spawner.csv", tf = "job_spawner_task.csv";	// logical names
	if (cached_js_csv.empty())
		return;
	// parents: job_source_name, train_group, start_time, stop_time, interval_distribution, dist_prm1, dist_prm2
	map<string, JobSpawnerBase*> by_name;
	for (int i = 1; i < (int)cached_js_csv.size(); ++i) {
		csv_check::require_cols(cached_js_csv, i, 7, pf);
		string name = cached_js_csv[i][0];
		string group = cached_js_csv[i][1];
		double start = csv_check::parse_double(cached_js_csv[i][2], pf, i, "start_time");
		const string& sv = cached_js_csv[i][3];
		double stop = (sv.empty() || sv == "NA") ? -1 : csv_check::parse_double(sv, pf, i, "stop_time");
		string distr = cached_js_csv[i][4];
		double p1 = csv_check::parse_double(cached_js_csv[i][5], pf, i, "dist_prm1");
		double p2 = csv_check::parse_double(cached_js_csv[i][6], pf, i, "dist_prm2");
		JobSpawnerBase* js = new JobSpawnerBase(name, this, group, start, stop, distr, p1, p2);
		job_spawners.push_back(js);
		if (by_name.count(name))
			throw runtime_error(csv_check::ctx(pf, i, "job_source_name") + ": duplicate '" + name + "'");
		by_name[name] = js;
	}//i

	// child tasks (long format): job_source_name, position, terminal, dwell_distribution, dist_prm1, dist_prm2
	// Collect per parent, order by position, then add in order.
	if (!cached_jst_csv.empty()) {
		struct Row { int pos; Terminal* term; string distr; double p1; double p2; };
		map<string, vector<Row>> rows_by;
		for (int i = 1; i < (int)cached_jst_csv.size(); ++i) {
			csv_check::require_cols(cached_jst_csv, i, 6, tf);
			string jsname = cached_jst_csv[i][0];
			if (!by_name.count(jsname))
				throw runtime_error(csv_check::ctx(tf, i, "job_source_name")
					+ ": '" + jsname + "' has no matching row in " + pf);
			int pos = csv_check::parse_int(cached_jst_csv[i][1], tf, i, "position");
			Terminal* term = csv_check::lookup(network->terminal_map, cached_jst_csv[i][2], tf, i, "terminal");
			string ddistr = cached_jst_csv[i][3];
			double dp1 = csv_check::parse_double(cached_jst_csv[i][4], tf, i, "dist_prm1");
			double dp2 = csv_check::parse_double(cached_jst_csv[i][5], tf, i, "dist_prm2");
			rows_by[jsname].push_back({ pos, term, ddistr, dp1, dp2 });
		}//i
		for (auto& [jsname, rows] : rows_by) {
			std::sort(rows.begin(), rows.end(), [](const Row& a, const Row& b) { return a.pos < b.pos; });
			for (auto& r : rows)
				by_name[jsname]->add_task(r.term, r.distr, r.p1, r.p2);
		}
	}
}


//DistributionSampler implementation============================
DistributionSampler::DistributionSampler(std::mt19937& _rng, 
	string _distr_name, double prm1, double prm2) 
	:
	rng(_rng), distr_name(_distr_name),
	constant(prm1), 
	ufm(nullptr), nrm(nullptr), exp(nullptr)
{
	if (distr_name == string("constant"))
		distr = [this]() { return constant; };
	else if (distr_name == string("uniform")) {
		ufm = new std::uniform_real_distribution<>(prm1, prm2);
		distr = [this]() { return (*ufm)(rng); };
	}
	else if (distr_name == string("normal")) {
		nrm = new std::normal_distribution<>(prm1, prm2);
		distr = [this]() { return (*nrm)(rng); };
	}
	else if (distr_name == string("exponential")) {
		exp = new std::exponential_distribution<>(prm1);
		distr = [this]() { return (*exp)(rng); };
	}
	else
		throw runtime_error("DistributionSampler: unrecognised distribution type '" + distr_name + "'");
}

double DistributionSampler::sample() {
	return distr();
}

//OpenLoopTrainSpawner implementation=========================

OpenLoopTrainSpawner::OpenLoopTrainSpawner(string _name, RailSimMaster* _master,
	int spawn_seg, int spawn_head,
	TrainTemplate* _train_template,
	string _distr, double _dist_prm1, double _dist_prm2)
	:
	SimObject(_name, _master->sim),
	name(_name),
	master(_master),
	train_template(_train_template),
	distr(_distr), dist_prm1(_dist_prm1), dist_prm2(_dist_prm2)
{
	spawn_arc = master->network->segments[spawn_seg]->get_arc(spawn_head);
	if (!spawn_arc)
		throw runtime_error("OpenLoopTrainSpawner '" + _name + "': segment "
			+ to_string(spawn_seg) + " has no arc with head node " + to_string(spawn_head));
	//activate();
}

OpenLoopTrainSpawner::~OpenLoopTrainSpawner()
{
	delete iat;
}

void OpenLoopTrainSpawner::add_task(string terminal, bool stop, double dwell_time,
	double earliest_departure) {
	job_template.emplace_back(master->network->terminal_map[terminal], stop, dwell_time,
		earliest_departure);
}

SimCoroutine OpenLoopTrainSpawner::run() {
	iat = new DistributionSampler(master->rng, distr, dist_prm1, dist_prm2);
	// Effective spawn-stop time: this spawner's own stop_time if set, else the
	// sim-wide start_warmdown, else never. Emissions cease at/after this time.
	double eff_stop = (stop_time >= 0) ? stop_time
		: (master->start_warmdown > 0 ? master->start_warmdown : -1);
	double t_prev = 0;
	while (true) {
		double rnd_iat = iat->sample();
		double t_next = t_prev + rnd_iat;
		if (eff_stop >= 0 && ge(t_next, eff_stop, Train::eps))
			co_return;	// stop spawning; the sampled arrival lands in the drain window
		if (gt(t_next, sim.time_now, Train::eps)) {
			double dt = t_next - sim.time_now;
			co_await delay(dt);
		}

		int debug = 1;

		if (spawn_arc->segment->state != TrackSegment::FREE)
			co_await wait_until([this]() { return spawn_arc->segment->state == TrackSegment::FREE; });
		
		string str = name + to_string(ols_spawn_ctr);
		Train* tr = new Train(str, sim, false,
			train_template->max_spd,
			train_template->accel,
			train_template->decel,
			master->network);
		for (auto car : train_template->consist)
			tr->add_car(new RollingStock(car->name, car->length));
		trains.push_back(tr);

		tr->job = new Job();
		tr->job->service_name = name;
		tr->job->creation_time = t_next;	//open-loop: assigned at spawn, so no queue wait
		for (auto task : job_template)
			tr->job->add_task(task.terminal, task.stop, task.dwell_time, task.earliest_departure);
		
		double pos = spawn_arc->segment->length - tr->length;
		tr->place(spawn_arc, pos);
		tr->spawn_scheduled_time = t_next;
		tr->activate();

		if (master->animate)
			TrainSprite* sprite = new TrainSprite(master, tr, train_template);

		++ols_spawn_ctr;
		t_prev = t_next;
	}//wend
	co_return;
}

//ClosedLoopTrainSpawner implementation=========================

ClosedLoopTrainSpawner::ClosedLoopTrainSpawner(string _train_group, RailSimMaster* _master,
	TrainTemplate* _train_template, Terminal* _spawn_terminal,
	double _spawn_interval, int _spawn_count)
	:
	SimObject(_train_group, _master->sim),
	name(_train_group),
	master(_master),
	train_group(_train_group),
	train_template(_train_template),
	spawn_terminal(_spawn_terminal),
	spawn_interval(_spawn_interval),
	spawn_count(_spawn_count)
{
}

SimCoroutine ClosedLoopTrainSpawner::run() {
	if (spawn_terminal->arcs.empty())
		throw runtime_error("ClosedLoopTrainSpawner '" + name + "': spawn terminal has no arcs to spawn on");
	DirectedSegment* spawn_arc = spawn_terminal->arcs[0];	// derive entry arc from terminal

	for (int k = 0; k < spawn_count; ++k) {
		if (gt(spawn_interval, 0.0, Train::eps))
			co_await delay(spawn_interval);
		// serialise fleet entry: wait for the spawn segment to clear
		if (spawn_arc->segment->state != TrackSegment::FREE)
			co_await wait_until([spawn_arc]() { return spawn_arc->segment->state == TrackSegment::FREE; });

		string str = train_group + "_" + to_string(cls_spawn_ctr);
		Train* tr = new Train(str, sim, true,			// persistent = true
			train_template->max_spd,
			train_template->accel,
			train_template->decel,
			master->network,
			master->manager);						// wired to the dispatcher
		tr->train_group = train_group;
		for (auto car : train_template->consist)
			tr->add_car(new RollingStock(car->name, car->length));
		trains.push_back(tr);

		// NB: no pre-built job -- Train::run requests one via manager->ready_signal.
		double pos = spawn_arc->segment->length - tr->length;
		tr->place(spawn_arc, pos);
		tr->spawn_scheduled_time = sim.time_now;
		tr->activate();

		if (master->animate)
			TrainSprite* sprite = new TrainSprite(master, tr, train_template);

		++cls_spawn_ctr;
	}//k
	co_return;
}


//JobSpawnerBase implementation=================================

JobSpawnerBase::JobSpawnerBase(string _name, RailSimMaster* _master, string _train_group,
	double _start_time, double _stop_time,
	string _interval_distr, double _int_prm1, double _int_prm2)
	:
	SimObject(_name, _master->sim),
	name(_name),
	master(_master),
	train_group(_train_group),
	start_time(_start_time),
	stop_time(_stop_time),
	interval_distr(_interval_distr),
	int_prm1(_int_prm1),
	int_prm2(_int_prm2)
{
}

JobSpawnerBase::~JobSpawnerBase() {
	delete iat;
	for (auto ds : dwell_samplers)
		delete ds;
	dwell_samplers.clear();
}

void JobSpawnerBase::add_task(Terminal* terminal, string dwell_distr, double prm1, double prm2) {
	task_specs.push_back({ terminal, dwell_distr, prm1, prm2 });
}

SimCoroutine JobSpawnerBase::run() {
	iat = new DistributionSampler(master->rng, interval_distr, int_prm1, int_prm2);
	for (auto& ts : task_specs)
		dwell_samplers.push_back(new DistributionSampler(master->rng, ts.dwell_distr, ts.dwell_prm1, ts.dwell_prm2));

	// Effective stop time: this source's own stop_time if set, else start_warmdown, else never.
	double eff_stop = (stop_time >= 0) ? stop_time
		: (master->start_warmdown > 0 ? master->start_warmdown : -1);

	if (gt(start_time, sim.time_now, Train::eps))
		co_await delay(start_time - sim.time_now);

	double t_prev = sim.time_now;
	while (true) {
		double t_next = t_prev + iat->sample();
		if (eff_stop >= 0 && ge(t_next, eff_stop, Train::eps))
			co_return;	// stop emitting
		if (gt(t_next, sim.time_now, Train::eps))
			co_await delay(t_next - sim.time_now);

		Job* job = new Job();
		job->service_name = name;
		job->creation_time = sim.time_now;
		job->train_group = train_group;
		for (size_t i = 0; i < task_specs.size(); ++i) {
			double dwell = dwell_samplers[i]->sample();
			bool stop = (i + 1 == task_specs.size()) || gt(dwell, 0.0, Train::eps);
			job->add_task(task_specs[i].terminal, stop, dwell);
		}
		master->manager->submit_job(job);

		t_prev = t_next;
	}//wend
	co_return;
}


//TrainSprite implementation==============================

TrainSprite::TrainSprite(RailSimMaster* _master, Train* _train, TrainTemplate* _template)
	:
	SimObject(_train->descr + string("_a"), _master->sim),
	master(_master), train(_train), tr_template(_template), lock_colour(255,0,0), finished(false)
{
	uniform_int_distribution<> rand_rgb(50, 205);
	int r = rand_rgb(master->anim_rng);
	int g = rand_rgb(master->anim_rng);
	int b = rand_rgb(master->anim_rng);

	lock_colour.set_rgb(r, g, b);

	activate();
}

SimCoroutine TrainSprite::run() {
	record_state();

	co_await wait_until([this]() { return master->sim.time_now >= master->anim_start_t || train->finished; });
	if (train->finished)
		co_return;

	//initial placement of train sprite
	int for_start = master->render_cars ? tr_template->consist.size() - 1 : 0;
	for (int i = for_start; i >= 0; --i) {		//reverse order so loco layered on top
		obj_id = rs_id(i);
		auto* rs = tr_template->consist[i];
		master->script->add_object(obj_id, rs->spritef,
			rs->sprite_scale, 0.0, 0.0, false);
		master->script->set_object_guides(obj_id,
			rs->sprite_w / 2.0,
			rs->sprite_h, 0);
		master->script->set_object_true_length(obj_id, rs->length,
			master->render_sprites_to_scale);
		if (rs->colour_r != 255 || rs->colour_g != 255 || rs->colour_b != 255)
			master->script->set_object_image_colour(obj_id, rs->colour_r, rs->colour_g, rs->colour_b);
	}
	
	DirectedSegment* spawn_arc = train->arcs.front();
	master->script->place_object_on_path(obj_id, spawn_arc->str_id, true, (spawn_arc->segment->length - train->front_pos) / spawn_arc->segment->length);
	master->script->custom("update_arc_state", vector<string>({ spawn_arc->str_id, "1", lock_colour.rstr, lock_colour.gstr, lock_colour.bstr }));

	obj_id = rs_id(0);
	if (master->render_cars) {
		string leader_id = obj_id;
		for (int i = 1; i < train->cars.size(); i++) {
			string follower_id = rs_id(i);
			master->script->follow_leader(follower_id, leader_id);
			leader_id = follower_id;
		}
	}

	SpeedProfileArc* sp_arc = nullptr;
	bool new_spawn = true;		//this did't fix the problem
	while (true) {
		if (!new_spawn)
			co_await wait_until([this]() { return something_changed(); });

		// Skipped while a reversal is pending: Train::reverse has already flipped
		// `arcs`, so `arcs.back()` is now the OLD FRONT and this would release an
		// arc the train is still standing on -- the reversed consist then draws
		// with no occupancy at all until it wins its next section. Nothing is
		// released by a reversal (it happens at a stand, with the whole train
		// inside its section), and rebuild_consist() re-asserts below.
		if (train->reversal_count == rev_seen && b_arc != train->arcs.back()) {
			if (b_arc->owner == nullptr || b_arc->owner == train)	//this is a hack, should apply release dleay
				master->script->custom("update_arc_state", vector<string>({ b_arc->str_id, "0" }));
			//else if (b_arc->owner == train)
				//release_queue.push(b_arc);
		}

		//if (release_queue.size() > 0 && release_queue.front()->owner == nullptr) {
			//master->script->custom("update_arc_state", vector<string>({ release_queue.front()->str_id, "0" }));
			//release_queue.pop();
		//}

		// A reversal happens at a stand, so it has to be caught BEFORE the stationary
		// early-out below, which would otherwise just re-record state and wait.
		if (train->reversal_count != rev_seen) {
			rebuild_consist();
			record_state();
			new_spawn = false;
			co_await wait_until([this]() { return something_changed(); });
			continue;
		}

		if (f_arc != train->arcs.front() || train->sp_arc_ptr != sp_seen
			|| train->current_acceleration != a_inst || train->section != section || new_spawn) {
			if (eq(train->current_acceleration, 0, train->eps) && eq(train->v_inst, 0, train->eps)) {
				record_state();
				co_await wait_until([this]() { return something_changed(); });
				continue;   //train is stationary, nothing to do
			}

			if (train->section != section || new_spawn) {
				for (auto s_arc : train->section->arcs)
					master->script->custom("update_arc_state", vector<string>({ s_arc->str_id, "1", lock_colour.rstr, lock_colour.gstr, lock_colour.bstr }));
			}//if

			DirectedSegment* arc = train->arcs.front();
			sp_arc = train->sp_arc_ptr;

			double t = 0;
			double move_end = 1.0;

			if (gt(train->current_acceleration, 0, train->eps)) {
				t = sp_arc->t_accel;
				move_end = 1.0 - sp_arc->pos_coast / arc->segment->length;
			}
			else if (eq(train->current_acceleration, 0, train->eps)) {
				t = sp_arc->t_coast;
				move_end = 1.0 - sp_arc->pos_decel / arc->segment->length;

			}
			else { //decelerating
				t = sp_arc->t_decel;
				if (train->speed_profile.back().arc == arc && eq(train->speed_profile.back().v1, 0, Train::eps))
					move_end = 1.0 - arc->margin / arc->segment->length;
				else
					move_end = 1.0;
			}
			assert(ge(t, 0, train->eps));
			if (eq(t, 0, train->eps)) {
				record_state();
				new_spawn = false;
				continue;
			}

			master->script->place_object_on_path(obj_id, arc->str_id, true, (arc->segment->length - train->front_pos) / arc->segment->length);

			if (eq(train->current_acceleration, 0, train->eps)) {
				master->script->move_object_on_path(obj_id, train->arcs.front()->str_id, t, 1, 1, move_end);
			}
			else {
				double nrm_accel = train->current_acceleration / arc->segment->length;
				double nrm_v0 = train->v_inst / arc->segment->length;
				master->script->accel_object_on_path(obj_id, train->arcs.front()->str_id, t, 1, 1, move_end,
					nrm_accel, nrm_v0);
			}

		}
		else {

		}
		new_spawn = false;

		if (train->finished) {
			master->script->custom("update_arc_state", vector<string>({ b_arc->str_id, "0" }));
			for (int i = 0; i < train->cars.size(); i++) {
				master->script->delete_object(rs_id(i));
				if (!master->render_cars)
					break;
			}
			break;
		}

		if (sim.time_now > master->anim_end_t)
			break;
		record_state();
	}//wend

	finished = true;

	terminate();

	co_return;
}

string TrainSprite::rs_id(int i)
{
	// Object ids carry the reversal number, because they cannot be reused across one.
	// scenario_db's anim.py folds the script into flat per-id dicts (obj_add, obj_del,
	// leader) and resolves each follow chain ONCE for the whole run, so a reused id
	// would apply the post-reversal chain retroactively to the pre-reversal train and
	// merge both directions' motion into a single itinerary. With fresh ids the
	// builder simply sees one train end and another begin, at the same instant and
	// place -- which, with solid blocks, is seamless.
	string id = string("tr") + to_string(train->id) + train->cars[i]->model
		+ to_string(train->cars[i]->id);
	return rev_seen == 0 ? id : id + "_r" + to_string(rev_seen);
}

void TrainSprite::rebuild_consist()
{
	// `unfollow_leader` would be the tidier tool, but scenario_db's anim.py ignores it
	// ("latently broken"), so the consist is torn down and re-laid instead. Delete
	// before bumping rev_seen, so the ids match the ones that were added.
	int last = master->render_cars ? (int)tr_template->consist.size() - 1 : 0;
	for (int i = last; i >= 0; --i)
		master->script->delete_object(rs_id(i));

	rev_seen = train->reversal_count;

	// Physical order, front to back. `forward` records which end leads, so after an
	// odd number of reversals the last car is at the front.
	vector<int> order;
	if (train->forward)
		for (int i = 0; i <= last; ++i) order.push_back(i);
	else
		for (int i = last; i >= 0; --i) order.push_back(i);

	// add back to front so the leader ends up layered on top, as at spawn
	for (int k = (int)order.size() - 1; k >= 0; --k) {
		int ci = order[k];
		string oid = rs_id(ci);
		auto* rs = tr_template->consist[ci];
		master->script->add_object(oid, rs->spritef, rs->sprite_scale, 0.0, 0.0, false);
		master->script->set_object_guides(oid, rs->sprite_w / 2.0, rs->sprite_h, 0);
		master->script->set_object_true_length(oid, rs->length, master->render_sprites_to_scale);
		if (rs->colour_r != 255 || rs->colour_g != 255 || rs->colour_b != 255)
			master->script->set_object_image_colour(oid, rs->colour_r, rs->colour_g, rs->colour_b);
	}

	obj_id = rs_id(order.front());
	DirectedSegment* arc = train->arcs.front();
	master->script->place_object_on_path(obj_id, arc->str_id, true,
		(arc->segment->length - train->front_pos) / arc->segment->length);

	// Re-assert occupancy. The overlay is driven purely by emitted events and the
	// new sprite starts with no history: record_state() captures the CURRENT
	// section, so the `section != train->section` block in run() never fires for
	// it, and at spawn the equivalent lock is emitted explicitly. Without this the
	// reversed train shows as running on unlocked track.
	for (auto* occ : train->arcs)
		master->script->custom("update_arc_state", vector<string>({ occ->str_id, "1",
			lock_colour.rstr, lock_colour.gstr, lock_colour.bstr }));

	if (master->render_cars) {
		string leader_id = obj_id;
		for (size_t k = 1; k < order.size(); ++k) {
			string follower_id = rs_id(order[k]);
			master->script->follow_leader(follower_id, leader_id);
			leader_id = follower_id;
		}
	}
}

bool TrainSprite::something_changed()
{
	return section != train->section ||
		train->arcs.front() != f_arc ||
		train->arcs.back() != b_arc ||
		train->sp_arc_ptr != sp_seen ||		//a same-arc profile split: new leg, no new arc
		(release_queue.size()>0 && release_queue.front()->owner == nullptr) ||
		train->current_acceleration != a_inst ||
		train->section != section ||
		train->finished;
}

void TrainSprite::record_state()
{
	f_arc = train->arcs.front();
	b_arc = train->arcs.back();
	sp_seen = train->sp_arc_ptr;
	section = train->section;
	v_inst = train->v_inst;
	a_inst = train->current_acceleration;
	t_obs = sim.time_now;
}

PassingLoopGroup::PassingLoopGroup(int _id) :
	id(_id), appr(2), mainline(2), siding(2), ml_max_length(2, 0.0), sid_max_length(2, 0.0)
{
}

void PassingLoopGroup::configure(RailNetwork* network, bool create_constraints, int pl_constraint_mode)
{
	for (int i = 0; i < group.size(); i++) {
		DirectedSegment* arc = group[i];
		arc->is_signal = true;
		arc->margin = margins[i];
		if (gt(opp_speeds[i], 0, Train::eps)) {
			DirectedSegment* opp_arc = arc->segment->get_arc(arc->tail->id);
			opp_arc->speed_limit = opp_speeds[i];
			opp_arc->is_speed_change = true;
		}
	}

	for (auto arc : mainline) {
		arc->priority = -2;
		arc->lock_thru = true;
	}
	for (auto arc : siding)
		arc->priority = -1;
	for (auto arc : appr) {
		DirectedSegment* opp_arc = arc->segment->get_arc(arc->tail->id);
		opp_arc->priority = 2;
	}

	if (!create_constraints)
		return;

	if (pl_constraint_mode == 1) {
		// Segment-based PL constraints: 3 pairwise constraints per direction (6 total)
		// Each constraint has 2 SegTuples with null target — violated when both segments
		// occupied by trains with matching front_signal positions (same direction)
		// Optional max_length on mainline/siding tuples allows overlength trains to
		// stop at PLs with tail overhang without triggering constraints
		for (int dir = 0; dir < 2; ++dir) {
			DirectedSegment* arcs3[3] = { appr[dir], mainline[dir], siding[dir] };
			double max_lengths[3] = { 0.0, ml_max_length[dir], sid_max_length[dir] };
			string dir_str = "d" + to_string(dir);

			for (int p = 0; p < 3; ++p) {
				DirectedSegment* a = arcs3[p];
				DirectedSegment* b = arcs3[(p + 1) % 3];

				string cid = "plg_seg_" + to_string(id) + "_" + dir_str + "_" + to_string(p);
				SignalConstraint* sc = new SignalConstraint(cid, network, 1);

				// Build seg_tuples directly (no c_arcs, so dispatch goes to is_violated_seg)
				SignalConstraint::SegTuple st_a;
				st_a.segment = a->segment;
				st_a.front_signal = a;
				st_a.target = nullptr;
				st_a.max_length = max_lengths[p];
				sc->seg_tuples.push_back(st_a);

				SignalConstraint::SegTuple st_b;
				st_b.segment = b->segment;
				st_b.front_signal = b;
				st_b.target = nullptr;
				st_b.max_length = max_lengths[(p + 1) % 3];
				sc->seg_tuples.push_back(st_b);

				// Register on each unique front_signal arc's constraints list
				set<DirectedSegment*> registered;
				for (auto& st : sc->seg_tuples) {
					if (registered.find(st.front_signal) == registered.end()) {
						registered.insert(st.front_signal);
						assert(st.front_signal->is_signal);
						st.front_signal->constraints.push_back(sc);
					}
				}
			}
		}
		return;
	}

	// Arc-based PL constraints (default, mode 0)
	sc0 = new SignalConstraint(string("plg0") + to_string(id), network, 1);
	sc0->add_constraint_arc(mainline[0], nullptr);
	sc0->add_constraint_arc(siding[0], nullptr);
	sc0->add_constraint_arc(appr[0], nullptr);

	sc1 = new SignalConstraint(string("plg1") + to_string(id), network, 1);
	sc1->add_constraint_arc(mainline[1], nullptr);
	sc1->add_constraint_arc(siding[1], nullptr);
	sc1->add_constraint_arc(appr[1], nullptr);
}

void PassingLoopGroup::add_appr(int num, DirectedSegment* arc, double margin, double opp_speed)
{
	appr[num] = arc;
	group.push_back(arc);
	opp_speeds.push_back(opp_speed);
	margins.push_back(margin);
}

void PassingLoopGroup::add_mainline(int num, DirectedSegment* arc, double margin, double opp_speed, double max_length)
{
	mainline[num] = arc;
	group.push_back(arc);
	opp_speeds.push_back(opp_speed);
	margins.push_back(margin);
	ml_max_length[num] = max_length;
}

void PassingLoopGroup::add_siding(int num, DirectedSegment* arc, double margin, double opp_speed, double max_length)
{
	siding[num] = arc;
	group.push_back(arc);
	opp_speeds.push_back(opp_speed);
	margins.push_back(margin);
	sid_max_length[num] = max_length;
}

///////////////////////////////
TrainChart::TrainChart(int _id, RailNetwork* _network, double _bigM) :
	id(_id), network(_network),
	tclog("train_chart", true, true, false)
{
	big_m = _bigM;
	node_chg.resize(network->nodes.size(), _bigM);
	node_datum.resize(network->nodes.size(), 0.0);
	exit_nodes.resize(network->nodes.size(), false);
	init_log();
}

void TrainChart::add_start(int seg_id, int head_id, double base_chg)
{
	DirectedSegment* arc = network->segments[seg_id]->get_arc(head_id);
	if (!arc)
		throw runtime_error("train chart " + to_string(id) + ": start segment "
			+ to_string(seg_id) + " has no arc with head node " + to_string(head_id));
	starts.push_back({ arc, base_chg });
}

void TrainChart::add_exit_node(int node_id)
{
	exit_nodes[node_id] = true;
}

void TrainChart::add_entry_arc(int seg_id, int head_id)
{
	DirectedSegment* arc = network->segments[seg_id]->get_arc(head_id);
	entry_arcs.push_back(arc);
}

void TrainChart::initialise()
{
	list<TrackNode*> q;
	vector<bool> visited(network->nodes.size(), false);
	vector<bool> probed(network->nodes.size(), false);

	// Multi-source seed: each start arc pins its tail at base_chg and its head at
	// base_chg + length; the BFS relaxation (below) then takes the min at every
	// node, so parallel starts each measure from their own origin.
	for (auto& s : starts) {
		node_chg[s.arc->tail->id] = s.base_chg;
		node_datum[s.arc->tail->id] = s.base_chg;
		double hchg = s.base_chg + s.arc->segment->length;
		if (lt(hchg, node_chg[s.arc->head->id], Train::eps)) {
			node_chg[s.arc->head->id] = hchg;
			node_datum[s.arc->head->id] = s.base_chg;
		}
		visited[s.arc->tail->id] = true;
	}
	for (auto& s : starts) {
		TrackNode* hd = s.arc->head;
		if (!visited[hd->id] && !probed[hd->id]) {
			probed[hd->id] = true;
			q.push_back(hd);
		}
	}
	if (!q.empty()) {
		vector<TrackNode*> heap(q.begin(), q.end());
		make_heap(heap.begin(), heap.end(), NearerChainageFirst{ this });
		bfs_shortest_path(heap, visited, probed);
	}

	// Report what the corridor resolved to. A train chart is easy to break from a
	// distance -- any topology edit can move a chainage, and nothing else in the
	// output reveals it -- so make the extent visible on every run.
	int n_reached = 0;
	double max_chg = 0.0;
	for (size_t i = 0; i < node_chg.size(); ++i) {
		if (lt(node_chg[i], big_m, Train::eps)) {
			++n_reached;
			if (gt(node_chg[i], max_chg, Train::eps))
				max_chg = node_chg[i];
		}
	}
	cout << "train chart " << id << ": " << n_reached << " nodes, max chainage "
		<< max_chg << " km\n";

	check_chainage_continuity();

	// Monitor every arc the corridor can CHART, not just the hand-listed entries.
	// `entry` rows were a second, hand-maintained statement of the corridor
	// boundary, and it silently disagreed with the real one: a train that spawns
	// at a stub inside the corridor (or turns back at a platform in it) never
	// crosses a declared entry, so it never appeared -- while still occupying the
	// track and blocking everything behind it. The condition here is exactly
	// log_entry's own placement rule (at least one end reached), so the corridor
	// traces precisely what it is able to draw. Declared entries are still
	// honoured, in case one is deliberately placed outside the reached set.
	set<DirectedSegment*> monitored(entry_arcs.begin(), entry_arcs.end());
	for (auto segment : network->segments) {
		if (!segment)
			continue;
		bool tail_in = lt(node_chg[segment->node1->id], big_m, Train::eps);
		bool head_in = lt(node_chg[segment->node2->id], big_m, Train::eps);
		if (!tail_in && !head_in)
			continue;
		for (auto arc : { segment->get_arc(segment->node2->id),
						  segment->get_arc(segment->node1->id) })
			if (arc)
				monitored.insert(arc);
	}
	for (auto arc : monitored)
		new MonitorTrainChartEntry(this, arc);
	cout << "train chart " << id << ": monitoring " << monitored.size()
		<< " arcs for entry\n";
}

// Chainage is a shortest-path distance, so it is a METRIC over the track: for any
// segment joining two reached nodes, their chainages cannot differ by more than the
// segment is long. Dijkstra's relaxation guarantees that on the graph it searched, so
// a violation here means the search ran on a DIFFERENT graph from the one trains run
// on -- in practice an `exit` node sitting where trains actually pass through, which
// blocks the short path and forces the far side to be measured the long way round.
// The chart then interpolates the whole difference across one short segment and draws
// a near-vertical line: the "glitch" that is otherwise only findable by eye.
//
// Deliberately NOT a warning about multi-start corridors, which cannot trip this:
// taking the min over several datums is still a shortest-path metric, however many
// parallel platform roads a corridor starts from. An offset datum (a start with a
// non-zero base_chg) legitimately can, so the allowance carries that offset.
void TrainChart::check_chainage_continuity()
{
	int reported = 0;
	for (auto segment : network->segments) {
		if (!segment)
			continue;
		// a segment with no arcs is disabled in this scenario: no train can use it,
		// so the two sides being measured separately is not a fault
		if (segment->forward_arc == nullptr && segment->reverse_arc == nullptr)
			continue;
		int a = segment->node1->id, b = segment->node2->id;
		if (ge(node_chg[a], big_m, Train::eps) || ge(node_chg[b], big_m, Train::eps))
			continue;			//one side outside the corridor: nothing to compare
		// A start arc PINS its own two nodes (tail = base_chg, head = base_chg + its
		// length) rather than relaxing to them, so a SHORTER parallel road between the
		// same pair legitimately reads as a discontinuity -- the datum asserted the
		// long way round. That is a choice of datum, not a boundary cutting a live
		// path, and the corridor networks are full of it: spawn and exit roads run
		// between the same two nodes at different lengths and are a violation pair, so
		// no train ever runs one into the other.
		bool parallel_to_datum = false;
		for (auto& st : starts) {
			int sa = st.arc->tail->id, sb = st.arc->head->id;
			if ((a == sa && b == sb) || (a == sb && b == sa)) {
				parallel_to_datum = true;
				break;
			}
		}
		if (parallel_to_datum)
			continue;
		double gap = fabs(node_chg[a] - node_chg[b]);
		double allow = segment->length + fabs(node_datum[a] - node_datum[b]);
		if (le(gap, allow, Train::eps))
			continue;
		if (reported == 0)
			cout << "WARNING: train chart " << id << ": chainage is discontinuous -- a"
				 << " corridor boundary is cutting a path trains can still take.\n";
		if (++reported <= 10)
			cout << "         segment " << segment->id << " (nodes " << a << "," << b
				 << ", " << segment->length << " km) has chainages " << node_chg[a]
				 << " and " << node_chg[b] << " -- " << (gap - allow)
				 << " km more apart than the track between them"
				 << ((exit_nodes[a] || exit_nodes[b])
					 ? "; one of those nodes is a declared exit\n" : "\n");
	}
	if (reported > 10)
		cout << "         ... and " << (reported - 10) << " more\n";
}

void TrainChart::bfs_shortest_path(vector<TrackNode*>& heap, vector<bool>& visited, vector<bool>& probed) {
	// Dijkstra over nodes: always finalise the nearest un-finalised node, so a
	// node's chainage cannot be frozen at a value some other route would beat.
	// `probed` still guards against pushing a node twice; a node whose chainage
	// improves after it was pushed is simply re-heapified in place.
	NearerChainageFirst farther{ this };
	while (!heap.empty()) {
		pop_heap(heap.begin(), heap.end(), farther);
		TrackNode* nd = heap.back();
		heap.pop_back();
		if (visited[nd->id])
			continue;
		visited[nd->id] = true;
		if (exit_nodes[nd->id])
			continue;			//corridor boundary: reached, but not expanded through

		auto relax = [&](TrackNode* next_nd, TrackSegment* segment) {
			if (visited[next_nd->id])
				return;
			double path_len = node_chg[nd->id] + segment->length;
			if (lt(path_len, node_chg[next_nd->id], Train::eps)) {
				node_chg[next_nd->id] = path_len;
				node_datum[next_nd->id] = node_datum[nd->id];
				if (probed[next_nd->id]) {
					// its key improved; restore the heap order
					make_heap(heap.begin(), heap.end(), farther);
					return;
				}
			}
			if (!probed[next_nd->id]) {
				probed[next_nd->id] = true;
				heap.push_back(next_nd);
				push_heap(heap.begin(), heap.end(), farther);
			}
		};
		for (auto arc : nd->out_arcs)
			relax(arc->head, arc->segment);
		for (auto arc : nd->in_arcs)
			relax(arc->tail, arc->segment);
	}//wend
}

void TrainChart::init_log()
{
	tclog.add_static_field("corridor_id", to_string(id));
	tclog.add("train", "int");
	tclog.add("chainage", "double");
	tclog.add("arc", "string");
}

void TrainChart::log_entry(Train* train)
{
	DirectedSegment* arc = train->arcs.front();
	int head = arc->head->id;
	int tail = arc->segment->other_node(arc->head)->id;

	// Only interpolate between nodes the corridor actually reached. The trace logs
	// once more after the train leaves the corridor (the log precedes the break
	// test), and that arc can straddle the boundary, with node_chg still holding the
	// big_m sentinel at the far end. Interpolating against it emits a chainage of
	// ~1e6 that wrecks the axis of any plot built from this file.
	//
	// One end reached is still useful: the train is exiting, so pin it to the
	// boundary node and let its line run flat to the corridor edge. Neither end
	// reached means the train is wholly outside the corridor -- nothing to place.
	bool tail_known = lt(node_chg[tail], big_m, Train::eps);
	bool head_known = lt(node_chg[head], big_m, Train::eps);
	if (!tail_known && !head_known)
		return;

	double chng;
	if (tail_known && head_known)
		chng = node_chg[tail] + (node_chg[head]- node_chg[tail]) * (arc->segment->length - train->front_pos) / arc->segment->length;
	else
		chng = tail_known ? node_chg[tail] : node_chg[head];

	tclog.data("train", train->id);
	tclog.data("chainage", chng);
	tclog.data("arc", arc->str_id);
	tclog.write_current_record();
}

//////////////////
MonitorTrainChartEntry::MonitorTrainChartEntry(TrainChart* _tc, DirectedSegment* _arc) : 
	SimObject("MonitorTrainChartEntry", _tc->network->signals_mgr->sim), 
	tc(_tc), arc(_arc)
{
	activate();
}

SimCoroutine MonitorTrainChartEntry::run()
{
	while (true) {
		co_await wait_until([this]() { return arc->segment->state == TrackSegment::LOADED; });
		// A consist spans several arcs, so without this guard every monitored arc it
		// touches starts its own trace and the corridor logs the train two or more
		// times per sample (322 such duplicate rows in run 64 with only the declared
		// entries; far more once every corridor arc is watched).
		if (arc->owner && !tc->tracing.count(arc->owner))
			new TrainChartTrace(tc, arc->owner);
		co_await wait_until([this]() { return arc->segment->state != TrackSegment::LOADED; });
	}//wend
	co_return;
}

///////////////////////
int TrainChartTrace::obj_ctr = 0;

list<TrainChartTrace*> TrainChartTrace::active;

TrainChartTrace::TrainChartTrace(TrainChart* _tc, Train* _train) :
	SimObject("MonitorTrainChartTrain", _tc->network->signals_mgr->sim),
	tc(_tc), train(_train), prev_pos(-1), prev_acc(0), prev_arc(nullptr), id(obj_ctr++)
{
	active.push_back(this);
	tc->tracing.insert(train);
	activate();
}

SimCoroutine TrainChartTrace::run()
{
	//if (id == 7)
		//int debug = 1;

	exiting = false;
	something_changed();
	while (true) {
		if (tc->exit_nodes[train->arcs.front()->head->id])
			exiting = true;
		log_now();
		// Stop tracing when the train leaves the corridor (reaches/stops at an exit
		// node, or moves off the exit arc) OR when it finishes/despawns. The finished
		// case is essential: `finished` is a PERMANENT flag, so something_changed()
		// (which treats it as a change) returns true forever once set -- without this
		// break the wait_until below returns immediately every iteration and the loop
		// spins at the finish instant, re-logging the final position and flooding
		// train_chart.csv (a train that finishes at a node NOT in the chart's exit set
		// -- e.g. an inner_brisbane entry-arc train -- never hits the exit break).
		if (train->finished ||
			(exiting && (eq(train->v_inst, 0.0, Train::eps) || !tc->exit_nodes[train->arcs.front()->head->id])))
			break;

		co_await wait_until([this]() { return something_changed(); });
	}//wend

	//cout << id << endl;
	//if (id == 7)
		//int debug = 1;

	active.remove(this);		// normal corridor exit: drop from the end-of-run flush set
	tc->tracing.erase(train);	// ...and let the corridor trace it again if it re-enters
	terminate();
	co_return;
}

void TrainChartTrace::flush_active()
{
	// At the horizon, every trace still registered belongs to a train that never
	// exited the corridor -- either mid-transit or permanently deadlocked. Emit one
	// more sample at each train's current position; because the row is timestamped
	// with sim.time_now (== max_time here), a stalled train's line extends flat to
	// the right edge of the chart instead of stopping dead at its last movement.
	for (auto trace : active)
		trace->log_now();
}

void TrainChartTrace::log_now()
{
	// Two things about a train can change at the SAME sim instant (its front arc
	// and its acceleration, say), waking something_changed() twice and writing the
	// identical row twice -- 322 such rows in run 64. Harmless to a plot, but it
	// makes the log's row count meaningless and any per-sample analysis wrong.
	if (eq(sim.time_now, last_log_t, Train::eps))
		return;
	last_log_t = sim.time_now;
	tc->log_entry(train);
}

bool TrainChartTrace::something_changed()
{
	if (not eq(train->front_pos, prev_pos, Train::eps) || 
		train->arcs.front() != prev_arc || train->finished ||
		train->current_acceleration != prev_acc) 
	{
		if (train->id == 113)
			int debug = true;

		prev_pos = train->front_pos;
		prev_arc = train->arcs.front();
		prev_acc = train->current_acceleration;
		return true;
	}
	return false;
}
