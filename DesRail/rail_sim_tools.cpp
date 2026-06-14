#include "rail_sim_tools.h"
#include <string>
#include <map>
#include <random>
#include <cassert>
#include <functional>
#include <queue>
#include <chrono>
#include "read_csv.h"
#include "desrail.h"
#include "comparison_tolerance.h"
#include "desviz_src/desviz_script.h"

using namespace std;
using namespace DesRail;
using namespace comparison_tolerance;

//RailSimMaster implementation==============================

int OpenLoopTrainSpawner::ols_spawn_ctr = 0;

RailSimMaster::RailSimMaster(string _input_dir, string _output_dir, int seed) :
	sim(0), input_dir(_input_dir), output_dir(_output_dir), 
	animate(false), random_seed(seed), anim_rng(1) {

	OpenLoopTrainSpawner::ols_spawn_ctr = 0;

	auto start = chrono::high_resolution_clock::now();
	initialise();
	auto end = chrono::high_resolution_clock::now();

	chrono::duration<double> elapsed = end - start;
	sim_init_time = elapsed.count();
}

RailSimMaster::~RailSimMaster()
{
	delete network;
	for (auto* ols : open_loop_spawners)
		delete ols;
	open_loop_spawners.clear();
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
	for (auto ols : open_loop_spawners)
		ols->activate();

	sim.run();

	auto end = chrono::high_resolution_clock::now();
	chrono::duration<double> elapsed = end - start;
	sim_run_time = elapsed.count();

	if (animate)
		script->write();

	return DesRail::mlog_master;
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
	network = read_network(input_dir + "network.csv");
	read_signals(network, input_dir + "signals.csv");
	read_signal_constraints(network, input_dir + "signal_constraints.csv");
	read_speed_limits(network, input_dir + "speed_limits.csv");
	read_violations(network, input_dir + "violations.csv");
	read_priorities(network, input_dir + "priorities.csv");
	read_parameters(network, input_dir + "general_params.csv");
	network->initialise_network();

	read_passing_loops(input_dir + "passing_loops.csv");

	network->process_network(sim);

	read_rs_templates(input_dir + "rs_templates.csv");
	read_train_templates(input_dir + "train_templates.csv");
	read_open_loop_spawners(input_dir + "open_loop_spawners.csv");
	read_runctrl(input_dir + "runctrl.csv");

	if (rst_debug)
		write_sections(network, output_dir + "sections.csv");
}

void RailSimMaster::read_passing_loops(string fname)
{
	PassingLoopGroup* plg = nullptr;
	vector<vector<string>> csv = read_csvf(fname);
	int prev_id = -1;
	for (int i = 1; i < csv.size(); i++) {
		int id = stoi(csv[i][0]);
		if (id != prev_id) {
			plg = new PassingLoopGroup(id);
			pl_groups.push_back(plg);
		}
		prev_id = id;
		int seg = stoi(csv[i][1]);
		int hd = stoi(csv[i][2]);
		double margin = stod(csv[i][3]);
		double opp_speed = stod(csv[i][4]);
		DirectedSegment* arc = network->segments[seg]->get_arc(hd);
		if (csv[i][5] == "APPR0")
			plg->add_appr(0, arc, margin, opp_speed);
		else if (csv[i][5] == "APPR1")
			plg->add_appr(1, arc, margin, opp_speed);
		else if (csv[i][5] == "ML0")
			plg->add_mainline(0, arc, margin, opp_speed);
		else if (csv[i][5] == "ML1")
			plg->add_mainline(1, arc, margin, opp_speed);
		else if (csv[i][5] == "S0")
			plg->add_siding(0, arc, margin, opp_speed);
		else if (csv[i][5] == "S1")
			plg->add_siding(1, arc, margin, opp_speed);
	}
	for (auto p : pl_groups)
		p->configure(network);
}//read_passing_loops

void RailSimMaster::read_rs_templates(string fname) {
	vector<vector<string>> rs_csv = read_csvf(fname);

	for (int i = 1; i < rs_csv.size(); ++i) {
		rs_templates[rs_csv[i][0]] = new RollingStockTemplate(rs_csv[i][0], stod(rs_csv[i][1]),
			rs_csv[i][2], stod(rs_csv[i][3]), stod(rs_csv[i][4]), stod(rs_csv[i][5]));
	}
}//read_rs_templates

void RailSimMaster::read_train_templates(string fname) {
	vector<vector<string>> tt_csv = read_csvf(fname);

	for (int i = 1; i < tt_csv.size(); ++i) {
		train_templates[tt_csv[i][0]] = new TrainTemplate(tt_csv[i][0],
			stod(tt_csv[i][1]), stod(tt_csv[i][2]), stod(tt_csv[i][3]));
		TrainTemplate& tt = *(train_templates[tt_csv[i][0]]);
		for (int col = 4; col < tt_csv[i].size(); col += 2) {
			if (tt_csv[i][col] == "")
				break;
			int n = stoi(tt_csv[i][col]);
			RollingStockTemplate* rst = rs_templates[tt_csv[i][col + 1]];
			for (int j = 0; j < n; ++j)
				tt.append_rolling_stock(rst);
		}//k
	}//i
}//read_train_templates

void RailSimMaster::read_open_loop_spawners(string fname) {
	vector<vector<string>> ols_csv = read_csvf(fname);

	for (int i = 1; i < ols_csv.size(); ++i) {
		string name = ols_csv[i][0];
		int spawn_seg = stoi(ols_csv[i][1]);
		int spawn_head = stoi(ols_csv[i][2]);
		string tt = ols_csv[i][3];
		string distr = ols_csv[i][4];
		double prm1 = stod(ols_csv[i][5]);
		double prm2 = stod(ols_csv[i][6]);

		open_loop_spawners.push_back(new OpenLoopTrainSpawner(name, this, spawn_seg, spawn_head, train_templates[tt], distr, prm1, prm2));
		OpenLoopTrainSpawner& ols = *(open_loop_spawners.back());
		for (int j = 7; j < ols_csv[i].size(); j += 2) {
			string terminal = ols_csv[i][j];
			if (terminal != "NA") {
				double dwell = stod(ols_csv[i][j + 1]);
				bool stop = (j >= ols_csv[i].size() - 2) || gt(dwell, 0);
				ols.add_task(terminal, stop, dwell);
			}
		}//j
	}//i
}

void RailSimMaster::read_runctrl(string fname)
{
	vector<vector<string>> csv = read_csvf(fname);
	double sim_len;

	for (int i = 1; i < csv.size(); ++i) {
		string label = csv[i][0];
		if (label == "sim_len") {
			sim_len = stod(csv[i][1]);
			sim.set_max_time(sim_len);
		}
		else if (label == "warmup") {
			//warmup period for stats collection
			double warmup = stod(csv[i][1]);
			lfu::initialise_log_globals(&sim, warmup);
		}
		else if (label == "screen_output") {
			//screen output verbosity
			Train::write_log_to_screen = false;
			Train::write_metric_to_screen = false;
			int verbosity = stoi(csv[i][1]);
			if (verbosity == 2)
				Train::write_log_to_screen = true;
			else if (verbosity == 1)
				Train::write_metric_to_screen = true;
		}
		else if (label == "animate") {
			//animation flag
			int anim_flag = stoi(csv[i][1]);
			if (anim_flag > 0)
				set_animation("../Animation/anim_script.json", 0, sim_len, false);
		}
		else if (label == "log_output") {
			//log file output verbosity
			Train::write_train_log = false;
			Train::write_metric_log = true;
			int verbosity = stoi(csv[i][1]);
			if (verbosity == 2)
				Train::write_train_log = true;
			else if (verbosity == 0)
				Train::write_metric_log = false;
		}
		else if (label == "seed")
			random_seed = stoi(csv[i][1]);
		else if (label == "train_charts") {
			if (csv[i][1] != "0")
				read_train_charts(input_dir + "train_charts.csv");
		}
		else
			network->record_warning(nullptr, nullptr, string("read_runctrl: unrecognised run control label - ") + label);
	}//i
}

void RailSimMaster::read_train_charts(string fname)
{
	vector<vector<string>> csv = read_csvf(fname);

	for (int i = 1; i < csv.size(); ++i) {
		int id = i - 1;
		int seg = stoi(csv[i][2]);
		int head = stoi(csv[i][3]);
		DirectedSegment* start_arc = network->segments[seg]->get_arc(head);
		TrainChart* train_chart = new TrainChart(id, network, start_arc);
		train_charts.push_back(train_chart);
		int j = 4;
		while (csv[i][j] != "END") {
			int node = stoi(csv[i][j]);
			train_chart->add_exit_node(node);
			++j;
		}//wend
		++j;
		while (csv[i][j] != "END") {
			int seg_id = stoi(csv[i][j]);
			int head_id = stoi(csv[i][j + 1]);
			train_chart->add_entry_arc(seg_id, head_id);
			j += 2;
		}//wend
		train_chart->initialise();
	}//i
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
		assert(false);	//unrecognised distribution type
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
	//activate();
}

OpenLoopTrainSpawner::~OpenLoopTrainSpawner()
{
	delete iat;
}

void OpenLoopTrainSpawner::add_task(string terminal, bool stop, double dwell_time) {
	job_template.emplace_back(master->network->terminal_map[terminal], stop, dwell_time);
}

SimCoroutine OpenLoopTrainSpawner::run() {
	iat = new DistributionSampler(master->rng, distr, dist_prm1, dist_prm2);
	double t_prev = 0;
	while (true) {
		double rnd_iat = iat->sample();
		double t_next = t_prev + rnd_iat;
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
		for (auto task : job_template)
			tr->job->add_task(task.terminal, task.stop, task.dwell_time);
		
		double pos = spawn_arc->segment->length - tr->length;
		tr->place(spawn_arc, pos);
		tr->activate();

		if (master->animate)
			TrainSprite* sprite = new TrainSprite(master, tr, train_template);

		++ols_spawn_ctr;
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
		master->script->add_object(obj_id, tr_template->consist[i]->spritef, 
			tr_template->consist[i]->sprite_scale, 0.0, 0.0, false);
		master->script->set_object_guides(obj_id, 
			tr_template->consist[i]->sprite_w / 2.0, 
			tr_template->consist[i]->sprite_h, 0);
		master->script->set_object_true_length(obj_id, tr_template->consist[i]->length, 
			master->render_sprites_to_scale);
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

		if (b_arc != train->arcs.back()) {
			if (b_arc->owner == nullptr || b_arc->owner == train)	//this is a hack, should apply release dleay
				master->script->custom("update_arc_state", vector<string>({ b_arc->str_id, "0" }));
			//else if (b_arc->owner == train)
				//release_queue.push(b_arc);
		}

		//if (release_queue.size() > 0 && release_queue.front()->owner == nullptr) {
			//master->script->custom("update_arc_state", vector<string>({ release_queue.front()->str_id, "0" }));
			//release_queue.pop();
		//}

		if (f_arc != train->arcs.front() || train->current_acceleration != a_inst || train->section != section || new_spawn) {
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
	return string("tr") + to_string(train->id) + train->cars[i]->model + to_string(train->cars[i]->id);
}

bool TrainSprite::something_changed()
{
	return section != train->section ||
		train->arcs.front() != f_arc ||
		train->arcs.back() != b_arc ||
		(release_queue.size()>0 && release_queue.front()->owner == nullptr) ||
		train->current_acceleration != a_inst ||
		train->section != section ||
		train->finished;
}

void TrainSprite::record_state()
{
	f_arc = train->arcs.front();
	b_arc = train->arcs.back();
	section = train->section;
	v_inst = train->v_inst;
	a_inst = train->current_acceleration;
	t_obs = sim.time_now;
}

PassingLoopGroup::PassingLoopGroup(int _id) :
	id(_id), appr(2), mainline(2), siding(2)
{
}

void PassingLoopGroup::configure(RailNetwork* network)
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

	sc_all = new SignalConstraint(string("plg_all") + to_string(id), network, 2);
	for (auto arc : group)
		sc_all->add_constraint_arc(arc, nullptr);

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

void PassingLoopGroup::add_mainline(int num, DirectedSegment* arc, double margin, double opp_speed)
{
	mainline[num] = arc;
	group.push_back(arc);
	opp_speeds.push_back(opp_speed);
	margins.push_back(margin);
}

void PassingLoopGroup::add_siding(int num, DirectedSegment* arc, double margin, double opp_speed)
{
	siding[num] = arc;
	group.push_back(arc);
	opp_speeds.push_back(opp_speed);
	margins.push_back(margin);
}

///////////////////////////////
TrainChart::TrainChart(int _id, RailNetwork* _network, DirectedSegment* _start_arc, double _bigM) :
	id(_id), network(_network), start_arc(_start_arc),
	tclog("train_chart", true, true, false)
{
	node_chg.resize(network->nodes.size(), _bigM);
	exit_nodes.resize(network->nodes.size(), false);
	init_log();
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
	node_chg[start_arc->tail->id] = 0;
	node_chg[start_arc->head->id] = start_arc->segment->length;
	list<TrackNode*> q;
	q.push_back(start_arc->head);
	vector<bool> visited(network->nodes.size(), false);
	vector<bool> probed(network->nodes.size(), false);
	visited[start_arc->tail->id] = true;
	bfs_shortest_path(q, visited, probed);

	for (auto arc : entry_arcs)
		new MonitorTrainChartEntry(this, arc);
}

void TrainChart::bfs_shortest_path(list<TrackNode*>& q, vector<bool>& visited, vector<bool>& probed) {
	TrackNode* nd = q.front();
	q.pop_front();
	visited[nd->id] = true;
	if (exit_nodes[nd->id])
		return;
	for (auto arc : nd->out_arcs) {
		TrackNode* next_nd = arc->head;
		if (!visited[next_nd->id]) {
			if (!probed[next_nd->id]) {
				probed[next_nd->id] = true;
				q.push_back(next_nd);
			}
			double path_len = node_chg[nd->id] + arc->segment->length;
			if (lt(path_len, node_chg[next_nd->id], Train::eps))
				node_chg[next_nd->id] = path_len;
		}
	}//arc
	for (auto arc : nd->in_arcs) {
		TrackNode* next_nd = arc->tail;
		if (!visited[next_nd->id]) {
			if (!probed[next_nd->id]) {
				probed[next_nd->id] = true;
				q.push_back(next_nd);
			}
			double path_len = node_chg[nd->id] + arc->segment->length;
			if (lt(path_len, node_chg[next_nd->id], Train::eps))
				node_chg[next_nd->id] = path_len;
		}
	}//arc
	while (q.size() > 0)
		bfs_shortest_path(q, visited, probed);
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
	double chng = node_chg[tail] + (node_chg[head]- node_chg[tail]) * (arc->segment->length - train->front_pos) / arc->segment->length;

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
		if (arc->owner)
			new TrainChartTrace(tc, arc->owner);
		co_await wait_until([this]() { return arc->segment->state != TrackSegment::LOADED; });
	}//wend
	co_return;
}

///////////////////////
int TrainChartTrace::obj_ctr = 0;

list<TrainChartTrace*> TrainChartTrace::tmp;

TrainChartTrace::TrainChartTrace(TrainChart* _tc, Train* _train) :
	SimObject("MonitorTrainChartTrain", _tc->network->signals_mgr->sim),
	tc(_tc), train(_train), prev_pos(-1), prev_acc(0), prev_arc(nullptr), id(obj_ctr++)
{
	//tmp.push_back(this);
	activate();
}

SimCoroutine TrainChartTrace::run()
{
	//if (id == 7)
		//int debug = 1;

	exiting = false;
	something_changed();
	while (true) {		
		if (train->id == 113)
			int debug = 1;
		
		if (tc->exit_nodes[train->arcs.front()->head->id])
			exiting = true;
		tc->log_entry(train);
		if (exiting && (eq(train->v_inst, 0.0, Train::eps) || !tc->exit_nodes[train->arcs.front()->head->id]))
			break;

		if (train->finished)
			int debug = 1;

		co_await wait_until([this]() { return something_changed(); });
	}//wend

	//cout << id << endl;
	//if (id == 7)
		//int debug = 1;

	terminate();
	co_return;
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
