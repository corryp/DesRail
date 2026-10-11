#include "desrail.h"
#include "env_util.h"
#include "comparison_tolerance.h"
#include <cmath>
using namespace comparison_tolerance;

namespace DesRail {
	int RollingStock::rolling_stock_counter;
	int Train::train_counter;
	int Task::task_counter;
	int Job::job_counter;
	int TrackSection::track_section_counter;
	int DirectedSegment::directed_segment_counter;
	int Terminal::terminal_counter;
	LogFile* mlog_master = nullptr;

	TrackNode::TrackNode(int _id) : id(_id) {}

	////////////////////////////////////////DirectedSegment

	DirectedSegment::DirectedSegment(TrackSegment* _segment, TrackNode* _head) :
		segment(_segment), head(_head), tail(_segment->other_node(_head)),
		is_signal(false), margin(0), lock_thru(false), is_terminal(false), is_speed_change(false),
		is_transient(false),
		terminal(nullptr), priority(0), pending_release(nullptr), owner(nullptr),
		id(directed_segment_counter++)
	{
		str_id = "(" + to_string(segment->id) + "-" + to_string(head->id) + ")";
	}

	DirectedSegment::~DirectedSegment()
	{
	}

	void DirectedSegment::lock(Train* _owner)
	{
		segment->update_state(TrackSegment::LOCKED, _owner);
		owner = _owner;
	}

	void DirectedSegment::release(Train* _owner)
	{
		segment->update_state(TrackSegment::FREE, _owner);
		owner = nullptr;
		if (pending_release != nullptr) {
			pending_release = nullptr;
		}
	}

	void DirectedSegment::delayed_release(Train* _owner)
	{
		assert(pending_release == nullptr && segment->state == TrackSegment::LOADED);
		pending_release = new DelayedTrackRelease(_owner, this);
	}

	void DirectedSegment::reset()
	{
		owner = nullptr;
		if (pending_release)
			delete pending_release;
		pending_release = nullptr;
	}


	////////////////////////////////////////TrackSegment

	TrackSegment::TrackSegment(int _id) : id(_id), owner(nullptr) {}
	
	TrackSegment::~TrackSegment()
	{
		if (forward_arc != nullptr) delete forward_arc;
		if (reverse_arc != nullptr) delete reverse_arc;
	}
	
	void TrackSegment::configure(TrackNode* _node1, TrackNode* _node2, double _length, bool _bidirectional, bool disabled) {
		node1 = _node1;
		node2 = _node2;
		bidirectional = _bidirectional;
		length = _length;

		forward_arc = nullptr;
		reverse_arc = nullptr;
		if (!disabled) {
			forward_arc = new DirectedSegment(this, node2);
			node1->out_arcs.push_back(forward_arc);
			node2->in_arcs.push_back(forward_arc);
			if (bidirectional) {
				reverse_arc = new DirectedSegment(this, node1);
				node1->in_arcs.push_back(reverse_arc);
				node2->out_arcs.push_back(reverse_arc);
			}
		}

		state = FREE;
	}//TrackSegment::configure

	TrackNode* TrackSegment::other_node(TrackNode* node) const {
		return node == node1 ? node2 : node1;
	}

	DirectedSegment* TrackSegment::get_arc(TrackNode* head) {
		return head == node2 ? forward_arc : reverse_arc;
	}

	DirectedSegment* TrackSegment::get_arc(int head_id) {
		return head_id == node2->id ? forward_arc : reverse_arc;
	}

	void TrackSegment::update_state(TrackState new_state, Train* _owner)
	{
		for (auto section : sections)
			section->notify_state_change(state, new_state);
		state = new_state;
		if (new_state == FREE)
			owner = nullptr;
		else
			owner = _owner;
	}

	void TrackSegment::reset()
	{
		state = FREE;
		owner = nullptr;
	}

	////////////////////////////////////////TrackSection

	TrackSection::TrackSection(int n_terminals) : 
		id(track_section_counter++), 
		reachable(n_terminals,false), 
		terminal(nullptr),
		length(0.0),
		priority(0.0),
		segments_locked_counter(0)
	{}

	void TrackSection::push_back(DirectedSegment* arc)
	{
		arcs.push_back(arc);
		if (arc->is_terminal)
			terminal = arc->terminal;
		arc->segment->sections.push_back(this);
		length += arc->segment->length;
		priority += arc->priority;
	}

	bool TrackSection::is_available()
	{
		return segments_locked_counter == 0;
	}

	void TrackSection::notify_state_change(TrackSegment::TrackState old_state, TrackSegment::TrackState new_state)
	{
		if (old_state == TrackSegment::FREE and new_state != TrackSegment::FREE)
			++segments_locked_counter;
		if (old_state != TrackSegment::FREE and new_state == TrackSegment::FREE)
			--segments_locked_counter;
	}

	void TrackSection::reset()
	{
		segments_locked_counter = 0;
	}

	////////////////////////////////////////Terminal

	Terminal::Terminal(string _name) : name(_name), id(terminal_counter++) {}

	void Terminal::add_arc(DirectedSegment* arc)
	{
		arcs.push_back(arc);
		arc->terminal = this;
	}

	////////////////////////////////////////RailNetwork

	RailNetwork* read_network(string path_name)
	{
		vector<vector<string>> segments_csv = read_csvf(path_name);
		if (segments_csv.size() < 2)
			throw runtime_error(path_name + ": network CSV is empty or header-only");

		// First pass: size the network to the max segment/node ID observed.
		int max_node = 0;
		int max_seg_id = -1;
		for (int i = 1; i < segments_csv.size(); i++) {
			csv_check::require_cols(segments_csv, i, 5, path_name);
			max_seg_id = max(csv_check::parse_int(segments_csv[i][0], path_name, i, "segment_id"), max_seg_id);
			max_node = max(csv_check::parse_int(segments_csv[i][1], path_name, i, "node1"), max_node);
			max_node = max(csv_check::parse_int(segments_csv[i][2], path_name, i, "node2"), max_node);
		}

		RailNetwork* network = new RailNetwork(max_node + 1, max_seg_id + 1);

		for (int i = 1; i < segments_csv.size(); i++) {
			int directionality = csv_check::parse_int(segments_csv[i][4], path_name, i, "directionality");
			int tail, head;
			if (directionality <= 1) {	//-1 = disabled, 0 = forward, 1 = bidirectional, 2 = reverse
				tail = csv_check::parse_id(segments_csv[i][1], network->nodes.size(), path_name, i, "tail_node");
				head = csv_check::parse_id(segments_csv[i][2], network->nodes.size(), path_name, i, "head_node");
			}
			else {
				tail = csv_check::parse_id(segments_csv[i][2], network->nodes.size(), path_name, i, "tail_node (reversed)");
				head = csv_check::parse_id(segments_csv[i][1], network->nodes.size(), path_name, i, "head_node (reversed)");
			}

			int seg_id = csv_check::parse_id(segments_csv[i][0], network->segments.size(), path_name, i, "segment_id");
			network->add_segment(
				seg_id,
				tail,
				head,
				csv_check::parse_double(segments_csv[i][3], path_name, i, "length"),
				directionality == 1,		//bidirectional flag
				directionality == -1		//disabled flag
			);
		}

		return network;
	}//read_network

	void read_signals(RailNetwork* network, string path_name) {
		vector<vector<string>> signals_csv = read_csvf(path_name);

		for (int i = 1; i < signals_csv.size(); i++) {
			csv_check::require_cols(signals_csv, i, 5, path_name);
			int s_id = csv_check::parse_id(signals_csv[i][0], network->segments.size(), path_name, i, "segment_id");
			int h_id = csv_check::parse_id(signals_csv[i][1], network->nodes.size(), path_name, i, "head_node_id");
			DirectedSegment* arc = network->segments[s_id]->get_arc(network->nodes[h_id]);
			if (!arc)
				throw runtime_error(csv_check::ctx(path_name, i, "arc lookup")
					+ ": segment " + to_string(s_id) + " has no arc with head node " + to_string(h_id));
			arc->is_signal = true;
			arc->margin = csv_check::parse_double(signals_csv[i][2], path_name, i, "margin");
			arc->lock_thru = csv_check::parse_int(signals_csv[i][3], path_name, i, "lock_thru") != 0;
			
			string terminal_str = signals_csv[i][4];
			if (terminal_str != "0" and terminal_str != "") {
				arc->is_terminal = true;
				bool found = false;
				for (auto terminal : network->terminals) {
					if (terminal->name == terminal_str) {
						found = true;
						terminal->add_arc(arc);
						break;
					}//if
				}//terminmal
				if (!found) {
					Terminal* terminal = network->add_terminal(terminal_str);
					terminal->add_arc(arc);
				}
			}//if
		}//i
	}//read_signals

	void read_signal_constraints(RailNetwork* network, string path_name)
	{
		vector<vector<string>> csv = read_csvf(path_name);
		SignalConstraint* sc = nullptr;

		string sc_id = "";
		for (int i = 1; i < csv.size(); i++) {
			csv_check::require_cols(csv, i, 4, path_name);
			if (csv[i][0] != sc_id) {
				sc_id = csv[i][0];
				int occ_limit = csv_check::parse_int(csv[i][1], path_name, i, "occupancy_limit");
				sc = new SignalConstraint(sc_id, network, occ_limit);
			}
			else {
				int s_id = csv_check::parse_id(csv[i][1], network->segments.size(), path_name, i, "segment_id");
				int h_id = csv_check::parse_id(csv[i][2], network->nodes.size(), path_name, i, "head_node_id");
				DirectedSegment* arc = network->segments[s_id]->get_arc(network->nodes[h_id]);
				if (!arc)
					throw runtime_error(csv_check::ctx(path_name, i, "arc lookup")
						+ ": segment " + to_string(s_id) + " has no arc with head node " + to_string(h_id));
				Terminal* terminal = nullptr;
				if (csv[i][3] != "NA")
					terminal = csv_check::lookup(network->terminal_map, csv[i][3], path_name, i, "terminal");
				sc->add_constraint_arc(arc, terminal);
			}
		}//i
	}
	
	void read_speed_limits(RailNetwork* network, string path_name)
	{
		vector<vector<string>> csv = read_csvf(path_name);

		for (int i = 1; i < csv.size(); i++) {
			csv_check::require_cols(csv, i, 3, path_name);
			int s_id = csv_check::parse_id(csv[i][0], network->segments.size(), path_name, i, "segment_id");
			int h_id = csv_check::parse_id(csv[i][1], network->nodes.size(), path_name, i, "head_node_id");
			DirectedSegment* arc = network->segments[s_id]->get_arc(network->nodes[h_id]);
			if (!arc)
				throw runtime_error(csv_check::ctx(path_name, i, "arc lookup")
					+ ": segment " + to_string(s_id) + " has no arc with head node " + to_string(h_id));

			//An arc carries ONE speed limit and one transient flag, and this loop assigns
			//straight into them, so a second row for the same arc silently overwrote the
			//first with no warning at all. That is always a mistake, and an expensive one:
			//a generated file that listed the crossover transients and then a persistent
			//limit for the same arcs quietly lost every crossover restriction, and the run
			//it produced was reported before the loss was noticed.
			if (arc->is_speed_change)
				throw runtime_error(csv_check::ctx(path_name, i, "duplicate arc")
					+ "arc (" + to_string(s_id) + "," + to_string(h_id) + ") already has a "
					"speed limit; an arc may appear only once in this file");
			arc->is_speed_change = true;
			arc->speed_limit = csv_check::parse_double(csv[i][2], path_name, i, "speed_limit");
			//Column 4 is `transient` (0/1), ahead of the comment so the data columns stay
			//contiguous: segment,head,speed_limit,transient,comment. A pre-2026-08-26 file has
			//its comment in that slot, which is worth saying plainly -- the alternative is an
			//"cannot parse 'western entrance to range crossing' as int" that tells nobody what
			//to do about it. A missing or blank column means an ordinary (persistent) limit.
			if (csv[i].size() > 3 && !csv_check::looks_like_int(csv[i][3]))
				throw runtime_error(csv_check::ctx(path_name, i, "transient")
					+ ": expected 0 or 1, found '" + csv[i][3] + "'. speed_limits.csv is now"
					+ " segment,head,speed_limit,transient,comment -- this looks like the older"
					+ " 4-column form with the comment in column 4; insert a 0 before it.");
			arc->is_transient = csv[i].size() > 3 && csv[i][3] != ""
				&& csv_check::parse_int(csv[i][3], path_name, i, "transient") != 0;
		}//i
	}//read_speed_limits

	void read_violations(RailNetwork* network, string path_name) {
		vector<vector<string>> vln_csv = read_csvf(path_name);

		for (int i = 1; i < vln_csv.size(); ++i) {
			csv_check::require_cols(vln_csv, i, 2, path_name);
			int seg1 = csv_check::parse_id(vln_csv[i][0], network->segments.size(), path_name, i, "segment1_id");
			int seg2 = csv_check::parse_id(vln_csv[i][1], network->segments.size(), path_name, i, "segment2_id");
			network->add_violation(seg1, seg2);
		}
	}

	// Optional: most datasets have no flat crossings modelled with dummy segments
	// and so ship no violation_triplets.csv. Absence is silent, not an error.
	//
	// Format mirrors violations.csv -- SEGMENTS, not arcs, and symmetric:
	//   segment1,segment2,segment3,comment
	// Three segments in order. Consecutive ones must share exactly one node, which
	// makes the arc sequence they describe unique, so naming the arcs explicitly
	// would add nothing. Like a violation, the prohibition is direction-free: a
	// turn the railway cannot make one way cannot be made the other way either, so
	// one row arms both traversals. Where a segment is one-way, the traversal that
	// needs its missing arc is skipped -- it is already impossible.
	void read_violation_triplets(RailNetwork* network, string path_name) {
		{
			ifstream probe(path_name);
			if (!probe.is_open())
				return;
		}
		vector<vector<string>> csv = read_csvf(path_name);

		for (int i = 1; i < csv.size(); ++i) {
			if (csv[i].empty() || csv[i][0].empty())
				continue;
			csv_check::require_cols(csv, i, 3, path_name);
			TrackSegment* seg[3];
			for (int k = 0; k < 3; ++k) {
				int s_id = csv_check::parse_id(csv[i][k], network->segments.size(),
					path_name, i, "segment" + to_string(k + 1) + "_id");
				seg[k] = network->segments[s_id];
			}//k

			// the joint between consecutive segments must be a single node, or the
			// path the row describes is not well defined
			TrackNode* joint[2];
			for (int k = 0; k < 2; ++k) {
				TrackNode* a1 = seg[k]->node1;
				TrackNode* a2 = seg[k]->node2;
				TrackNode* b1 = seg[k + 1]->node1;
				TrackNode* b2 = seg[k + 1]->node2;
				int n_shared = (a1 == b1) + (a1 == b2) + (a2 == b1) + (a2 == b2);
				if (n_shared != 1)
					throw runtime_error(csv_check::ctx(path_name, i, "segment sequence")
						+ ": segments " + to_string(seg[k]->id) + " and "
						+ to_string(seg[k + 1]->id) + " share "
						+ to_string(n_shared) + " nodes; exactly one is required so that "
						"the three segments describe a unique path");
				joint[k] = (a1 == b1 || a1 == b2) ? a1 : a2;
			}//k
			if (joint[0] == joint[1])
				throw runtime_error(csv_check::ctx(path_name, i, "segment sequence")
					+ ": segments " + to_string(seg[0]->id) + ","
					+ to_string(seg[1]->id) + "," + to_string(seg[2]->id)
					+ " double back through node " + to_string(joint[0]->id));

			// both traversals: forward through joint[0] then joint[1], and its reverse
			for (int rev = 0; rev < 2; ++rev) {
				TrackSegment* a = seg[rev ? 2 : 0];
				TrackSegment* c = seg[rev ? 0 : 2];
				TrackNode* j0 = rev ? joint[1] : joint[0];
				TrackNode* j1 = rev ? joint[0] : joint[1];
				DirectedSegment* x = a->get_arc(j0);
				DirectedSegment* y = seg[1]->get_arc(j1);
				DirectedSegment* z = c->get_arc(c->other_node(j1));
				if (x == nullptr || y == nullptr || z == nullptr)
					continue;	//a one-way segment makes this traversal impossible anyway
				if (y->is_signal)
					cout << "WARNING: violation triplet " << x->str_id << " -> "
						<< y->str_id << " -> " << z->str_id
						<< " has a signal on its middle arc, so it spans a section boundary "
						   "and cannot be enforced\n";
				network->add_violation_triplet(x->id, y->id, z->id);
			}//rev
		}//i
	}//read_violation_triplets

	void read_priorities(RailNetwork* network, string path_name)
	{
		vector<vector<string>> vln_csv = read_csvf(path_name);

		for (int i = 1; i < vln_csv.size(); ++i) {
			csv_check::require_cols(vln_csv, i, 3, path_name);
			int seg_id = csv_check::parse_id(vln_csv[i][0], network->segments.size(), path_name, i, "segment_id");
			int head_id = csv_check::parse_id(vln_csv[i][1], network->nodes.size(), path_name, i, "head_node_id");
			double priority = csv_check::parse_double(vln_csv[i][2], path_name, i, "priority");

			TrackSegment* seg = network->segments[seg_id];
			DirectedSegment* arc = seg->forward_arc->head->id == head_id ? seg->forward_arc : seg->reverse_arc;
			arc->priority = priority;
		}//i
	}

	void read_parameters(RailNetwork* network, string path_name)
	{
		vector<vector<string>> prm_csv = read_csvf(path_name);

		for (int i = 1; i < prm_csv.size(); ++i) {
			string prm_str = prm_csv[i][0];
			if (prm_str == "release_delay")
				network->release_delay = stod(prm_csv[i][1]);
			else if (prm_str == "signals_manager")
				network->signals_manager = prm_csv[i][1];
			else if (prm_str == "automatic_reverse_detect")
				network->automatic_reverse_detect = stoi(prm_csv[i][1]) != 0;
			else
				network->record_warning(nullptr, nullptr, string("read_parameters: unrecognised paramter string - ") + prm_str);
		}//i
	}

	void write_sections(RailNetwork* network, string path_name)
	{
		ofstream f(path_name);

		//'\n' not endl: endl flushes on every line, and this file has one line per arc per
		//section. On Blackwater/Moura that is 30.3 s of a 30.4 s "initialisation", almost all
		//of it flushing to a drvfs-mounted output directory, which was very nearly mistaken for
		//the cost of a section-discovery change.
		f << "id,segment,head,reachable\n";
		for (auto s : network->sections) {
			string reachable_list = "";
			for (int i = 0; i < network->terminals.size(); i++) {
				if (s->reachable[i])
					reachable_list += "," + network->terminals[i]->name;
			}//i
			for (auto a : s->arcs)
				f << s->id << "," << a->segment->id << "," << a->head->id << reachable_list << "\n";
		}//s

		f.close();
	}//write_sections

	bool RailNetwork::write_warnings_log = true;
	bool RailNetwork::write_warnings_to_screen = true;

	RailNetwork::RailNetwork(int n_nodes, int n_segments) :
		nodes(n_nodes, 0), segments(n_segments, 0), 
		violations(n_segments, set<int>()),
		release_delay(1.0/3600.0),
		warnings_log("warnings_log", write_warnings_log, true, write_warnings_to_screen)
	{
		RollingStock::rolling_stock_counter = 0;
		Train::train_counter = 0;
		Task::task_counter = 0;
		Job::job_counter = 0;
		TrackSection::track_section_counter = 0;
		DirectedSegment::directed_segment_counter = 0;
		Terminal::terminal_counter = 0;

		for (int i = 0; i < n_nodes; i++)
			nodes[i] = new TrackNode(i);
		for (int i = 0; i < n_segments; i++)
			segments[i] = new TrackSegment(i);
		if (write_warnings_log) {
			warnings_log.add("train_id", "int");
			warnings_log.add("arc", "string");
			warnings_log.add("warning", "string");
		}//if
	}//ctor

	RailNetwork::~RailNetwork() {
		for (int i = 0; i < terminals.size(); i++)
			delete terminals[i];
		for (int i = 0; i < sections.size(); i++)
			delete sections[i];
		for (int i = 0; i < nodes.size(); i++)
			delete nodes[i];
		for (int i = 0; i < segments.size(); i++)
			delete segments[i];
		delete signals_mgr;
		lfu::clear_master_logs();
		mlog_master = nullptr;
	}//dtor

	void RailNetwork::reset(Sim& new_sim)
	{
		if (signals_mgr) {
			signals_mgr->reset();
			delete signals_mgr;
		}
		for (auto s : sections) s->reset();
		for (auto arc : arcs) arc->reset();
		for (auto seg : segments) seg->reset();
		lfu::clear_master_logs();
		mlog_master = nullptr;

		signals_mgr = make_signals_manager(new_sim, this);
		signals_mgr->activate();
	}

	void RailNetwork::add_segment(int segment, int node1, int node2, double length, bool bidirectional, bool disabled) {
		segments[segment]->configure(nodes[node1], nodes[node2], length, bidirectional, disabled);
	}

	TrackSection* RailNetwork::add_section()
	{
		TrackSection* section = new TrackSection(terminals.size());
		sections.push_back(section);
		return section;
	}

	Terminal* RailNetwork::add_terminal(string name)
	{
		Terminal* terminal = new Terminal(name);
		terminals.push_back(terminal);
		terminal_map[name] = terminal;
		return terminal;
	}

	// True when joining `head` onto the end of `tail` would place a forbidden
	// triple across the boundary. Two straddles are possible: the triple's first
	// two arcs in `tail`, or only its first.
	bool RailNetwork::triplet_spans_join(const vector<DirectedSegment*>& tail,
		const vector<DirectedSegment*>& head)
	{
		if (violation_triplets.empty() || tail.empty() || head.empty())
			return false;
		size_t n = tail.size();
		if (n >= 2 && is_violation_triplet(tail[n - 2], tail[n - 1], head[0]))
			return true;
		if (head.size() >= 2 && is_violation_triplet(tail[n - 1], head[0], head[1]))
			return true;
		return false;
	}

	void RailNetwork::add_violation_triplet(int arc1_id, int arc2_id, int arc3_id)
	{
		violation_triplets.insert(make_tuple(arc1_id, arc2_id, arc3_id));
	}

	bool RailNetwork::is_violation_triplet(const DirectedSegment* a, const DirectedSegment* b,
		const DirectedSegment* c)
	{
		if (violation_triplets.empty() || a == nullptr || b == nullptr || c == nullptr)
			return false;
		return violation_triplets.find(make_tuple(a->id, b->id, c->id)) != violation_triplets.end();
	}

	void RailNetwork::add_violation(int segment1_id, int segment2_id)
	{
		violations[segment1_id].insert(segment2_id);
		violations[segment2_id].insert(segment1_id);
	}

	void RailNetwork::initialise_network()
	{
		//establish list of DirectedSegments and connectivity
		arcs.resize(DirectedSegment::directed_segment_counter);
		for (auto segment : segments) {
			if (segment->forward_arc != nullptr)
				arcs[segment->forward_arc->id] = segment->forward_arc;
			if (segment->reverse_arc != nullptr)
				arcs[segment->reverse_arc->id] = segment->reverse_arc;
		}//segment
		for (auto arc : arcs) {
			for (auto outarc : arc->head->out_arcs) {
				if (!is_violation(arc->segment->id, outarc->segment->id) && outarc != arc)
					arc->out_arcs.push_back(outarc);
			}//outarc
		}//arc
	}

	void RailNetwork::process_network(Sim &sim)
	{
		//Phase timings, so an initialisation cost can be attributed rather than guessed at.
		//Printed only when DESRAIL_TIME_INIT is set.
		const bool time_init = env_is_set("DESRAIL_TIME_INIT");
		auto mark = chrono::steady_clock::now();
		auto lap = [&](const char* what) {
			if (!time_init) return;
			auto now = chrono::steady_clock::now();
			cout << "[init] " << what << ": "
				<< chrono::duration<double>(now - mark).count() << " s\n";
			mark = now;
		};

		//depth first searches over arcs to establish TrackSections
		for (auto arc : arcs) {
			if (arc->is_signal) {
				vector<bool> visiting(arcs.size(), false);
				vector<bool> finished(arcs.size(), false);
				list<DirectedSegment*> path;

				get_sections(arc, arc, path, visiting, finished);
			}//if
		}//arc
		lap("section discovery");
		finalise_sections();
		lap("finalise_sections");

		//update adjacency lists for track sections
		for (auto s : sections) {
			for (auto dss : s->arcs.back()->out_sections) {
				if (is_violation(s->arcs.back()->segment->id, dss->arcs.front()->segment->id))
					continue;
				//a triple may straddle the boundary, with either one or two of its arcs
				//in the upstream section
				if (triplet_spans_join(s->arcs, dss->arcs))
					continue;
				s->downstream_sections.push_back(dss);
				dss->upstream_sections.push_back(s);
			}//dss
		}//s

		//reverse depth first searches over TrackSections to establish reachability
		for (auto terminal : terminals) {
			for (auto arc : terminal->arcs) {
				for (auto s : arc->in_sections) {
					vector<bool> visiting(sections.size(), false);
					vector<bool> finished(sections.size(), false);
					s->reachable[terminal->id] = true;
					back_propogate_reach(terminal, s, visiting, finished);
				}//s
			}//arc
		}//terminal

		signals_mgr = make_signals_manager(sim, this);
		signals_mgr->activate();
		lap("the rest (adjacency, reachability, signals manager)");
	}//porcess_network

	void RailNetwork::record_warning(Train* train, DirectedSegment* arc, string warning)
	{
		if (train != nullptr)
			warnings_log.data("train_id", train->id);
		else
			warnings_log.data("train_id", -1);
		if (arc != nullptr)
			warnings_log.data("arc", arc->str_id);
		else
			warnings_log.data("arc", -1);
		warnings_log.data("warning", warning);
		warnings_log.write_current_record();
	}

	//Diagnostic: set DESRAIL_TRACE_SECTIONS to a comma-separated list of SEGMENT ids to have
	//section discovery report every time it considers, rejects or accepts an arc on one of
	//those segments, and why. Added 2026-09-04 to find why a legal route through the Milton
	//crossovers produced no section. Off unless the variable is set.
	static const set<int>& section_trace_segments()
	{
		static set<int> ids;
		static bool loaded = false;
		if (!loaded) {
			loaded = true;
			string v;
			if (env_value("DESRAIL_TRACE_SECTIONS", v)) {
				string tok;
				stringstream ss(v);
				while (getline(ss, tok, ','))
					if (!tok.empty()) ids.insert(stoi(tok));
			}
		}
		return ids;
	}

	static void trace_section(const DirectedSegment* start, const DirectedSegment* arc,
		const DirectedSegment* outarc, size_t depth, const char* what)
	{
		if (section_trace_segments().count(outarc->segment->id) == 0)
			return;
		cout << "[sections] start(" << start->segment->id << "," << start->head->id << ")"
			<< " at(" << arc->segment->id << "," << arc->head->id << ")"
			<< " -> (" << outarc->segment->id << "," << outarc->head->id << ")"
			<< " depth=" << depth << "  " << what << "\n";
	}

	void RailNetwork::get_sections(
		DirectedSegment* start, 
		DirectedSegment* arc, 
		list<DirectedSegment*>& path, 
		vector<bool>& visiting, 
		vector<bool>& finished)
	{
		for (auto outarc : arc->out_arcs) {
			//`visiting` marks exactly the arcs on the path currently being extended, so this
			//test IS the cycle check and nothing else. It is no longer qualified by `finished`:
			//an arc whose subtree completed earlier keeps finished == true for the rest of this
			//start's search, so testing it here would suppress the warning for a genuine cycle
			//through any arc the search had already explored once.
			if (visiting[outarc->id]) {
				trace_section(start, arc, outarc, path.size(), "REJECTED: cycle on the current path");
				cout << "WARNING: network cycle detected at arc " << outarc->id << " (" << outarc->segment->id << "," << outarc->head->id << ")\n";
				continue;
			}

			if (is_violation(arc->segment->id, outarc->segment->id)) {
				trace_section(start, arc, outarc, path.size(), "REJECTED: pairwise violation");
				continue;
			}

			// Three-arc prohibition. The sequence built so far is [start] + path, so
			// `arc` is its last element and the element before it completes the triple.
			// When path is empty (arc == start) the triple's first arc lies upstream of
			// this section's start signal and is not visible here; read_violation_triplets
			// warns about the only configuration in which that can happen (a signal on the
			// triple's middle arc).
			if (!violation_triplets.empty()) {
				const DirectedSegment* prev = nullptr;
				if (path.size() >= 2) {
					auto it = path.end();
					advance(it, -2);
					prev = *it;
				}
				else if (path.size() == 1)
					prev = start;
				if (is_violation_triplet(prev, arc, outarc)) {
					trace_section(start, arc, outarc, path.size(), "REJECTED: violation triplet");
					continue;
				}
			}

			if (arc == start && outarc->start_sections.size() > 0) {
				for (auto s : outarc->start_sections) {
					// s begins with outarc, so its second arc completes a triple with
					// (start, outarc). Sections already discovered from a DIFFERENT start
					// are adopted here wholesale, so without this the prohibition would be
					// bypassed for exactly the legs it is meant to stop.
					if (s->arcs.size() >= 2 && is_violation_triplet(start, outarc, s->arcs[1]))
						continue;
					start->out_sections.push_back(s);
				}
				trace_section(start, arc, outarc, path.size(),
					"ADOPTED: reusing sections already discovered from this arc");
				continue;	//all sections starting from this arc already discovered
			}

			if (outarc->is_signal) {
				trace_section(start, arc, outarc, path.size(), "ACCEPTED: closes a section here");
				path.push_back(outarc);
				TrackSection* s = add_section();	//create a new section
				for (auto a : path)
					s->push_back(a);
				path.front()->start_sections.push_back(s);
				path.back()->in_sections.push_back(s);
				start->out_sections.push_back(s);
			}
			else {
				trace_section(start, arc, outarc, path.size(), "ACCEPTED: descending");
				path.push_back(outarc);
				visiting[outarc->id] = true;
				get_sections(start, outarc, path, visiting, finished);	//search deeper
				finished[outarc->id] = true;
				//Clear on backtrack, so `visiting` marks the arcs on the CURRENT path rather than
				//every arc this search has ever touched.
				//
				//Without this an arc could be entered only once per start signal, so the first
				//branch to reach it claimed it and every other route through it was silently
				//dropped. That is correct for reachability and wrong here, because a section is
				//a prefix plus a completion: two physically distinct routes that happen to share
				//one arc are two sections, and only one of them was ever built. Found via the
				//Milton crossovers, where the search descended into segment 74 and then died at
				//the 1 m flat-crossing dummy 309 because the parallel route through 217 had
				//reached 309 first. Neither crossover appeared in any section, so grant_access
				//could never offer them and their arc priorities were never consulted.
				//
				//Cost, measured 2026-09-04 once write_sections stopped flushing every line:
				//section discovery is about 1 ms even on Blackwater/Moura, whose section count
				//goes from 489 to 1,235. Corridor N20 is unchanged at 164, so the deadlock work
				//is untouched.
				visiting[outarc->id] = false;
			}
			path.pop_back();
		}//outarc
	}

	void RailNetwork::finalise_sections()
	{
		//create lock through sections
		int n = sections.size();
		for (int i = 0; i < n; i++) {
			TrackSection* s = sections[i];
			if (s->arcs.back()->lock_thru) {
				for (auto s2 : s->arcs.back()->out_sections) {
					if (is_violation(s->arcs.back()->segment->id, s2->arcs.front()->segment->id))
						continue;
					if (triplet_spans_join(s->arcs, s2->arcs))
						continue;	//concatenating them would build the forbidden turn
					TrackSection* snew = add_section();
					for (auto a : s->arcs)
						snew->push_back(a);
					for (auto a : s2->arcs)
						snew->push_back(a);
					snew->arcs.front()->start_sections.push_back(snew);
					snew->arcs.back()->in_sections.push_back(snew);
					for (auto a : snew->arcs.front()->tail->in_arcs) {
						if (a->is_signal)
							a->out_sections.push_back(snew);
					}
				}//s2
			}
		}//s

		//insert sections into ranked sets
		for (auto a : arcs) {
			for (auto s : a->out_sections)
				a->out_sections_ranked.insert(s);
		}
	}

	void RailNetwork::back_propogate_reach(Terminal* terminal, TrackSection* s, vector<bool>& visiting, vector<bool>& finished)
	{
		for (auto ups : s->upstream_sections) {

			//if (terminal->name == "TmbaStn_north" && ups->arcs.back()->segment->id == 172)
			//	int debug = 1;

			if (visiting[ups->id]) {
				if (!finished[ups->id]) {
					//warnings_log is a CSV: separate the parts with ';' so the message stays one field
					string str = string("cycle detected at section ") + to_string(ups->id) + "; " + terminal->name + "; " + s->arcs.front()->str_id;
					record_warning(nullptr, ups->arcs.back(), str);
					//cout << "WARNING: network cycle detected at section " << ups->id << endl;
				}
				continue;
			}

			if (ups->terminal != nullptr)
				continue;		//section ups ends in a terminal, nothing else reachable from there

			ups->reachable[terminal->id] = true;
			visiting[ups->id] = true;
			back_propogate_reach(terminal, ups, visiting, finished);
			finished[ups->id] = true;
		}//ups

	}

	void RailNetwork::update_network_state(TrackSegment* segment, TrackSegment::TrackState state)
	{
		segment->update_state(state);
		if (state == TrackSegment::FREE)
			signals_mgr->notify_segment_free();
	}

	bool RailNetwork::can_reach(DirectedSegment* from_arc, Terminal* target) const
	{
		if (from_arc == nullptr || target == nullptr)
			return false;
		// The reachability half of SignalsManager::grant_access, without
		// is_available() or the constraint checks. Those are about the state of the
		// network right now; this is about whether a route exists at all.
		for (auto section : from_arc->out_sections_ranked) {
			if (section->reachable[target->id])
				return true;
		}
		return false;
	}

	bool RailNetwork::is_violation(int segment1_id, int segment2_id)
	{
		return segment1_id == segment2_id || violations[segment1_id].find(segment2_id) != violations[segment1_id].end();
	}

	////////////////////////////////////////RollingStock

	RollingStock::RollingStock(string _model, double _length) : 
		id(rolling_stock_counter++), model(_model), length(_length) {}

	////////////////////////////////////////TrainUpdateTrigger
	TrainUpdateTrigger::TrainUpdateTrigger(Train* _train, UpdateType _update_type, double _delay_duration) :
		SimObject("TrainUpdateTrigger",_train->sim), train(_train), update_type(_update_type), 
		delay_duration(_delay_duration), trigger_time(_train->sim.time_now + _delay_duration),
		cancel_trigger(false)
	{
		activate();
	}

	void TrainUpdateTrigger::activate_trigger()
	{
		assert(asleep);
		wakeup();
	}

	SimCoroutine TrainUpdateTrigger::run()
	{
		if (delay_duration > -1)
			co_await delay(delay_duration);
		else
			co_await sleep();

		if (train->id == 8)
			int debug = 1;

		if (!cancel_trigger)
			train->action_update_trigger(this);
		else
			terminate();

		co_return;
	}

	////////////////////////////////////////DelayedTrackRelease

	DelayedTrackRelease::DelayedTrackRelease(Train* _train, DirectedSegment* _arc) :
		SimObject("DelayedTrackRelese", _train->sim), 
		train(_train), arc(_arc), duration(_train->network->release_delay),
		signals_mgr(_train->network->signals_mgr)
	{
		activate();
	}

	SimCoroutine DelayedTrackRelease::run()
	{
		arc->lock(train);
		co_await delay(duration);
		arc->release(train);
		signals_mgr->notify_segment_free();

		terminate();

		co_return;
	}

	////////////////////////////////////////Train

	bool Train::write_train_log = true;
	bool Train::write_metric_log = true;
	bool Train::write_log_to_screen = false;
	bool Train::write_metric_to_screen = false;
	bool Train::write_section_log = false;
	double Train::eps = 0.0000001;

	Train::Train(string _descr, Sim& _sim,
		bool _persistent, double _max_speed, double _accel, double _decel,
		RailNetwork* _network, TrainManagerBase* _manager)
		:
		SimObject(_descr, _sim), id(train_counter++),
		length(0),
		persistent(_persistent),
		max_speed(_max_speed),
		accel(_accel),
		decel(_decel),
		manager(_manager),
		network(_network),
		signals_mgr(_network->signals_mgr),
		job(nullptr),					// closed-loop trains have no pre-built job; the null guard in run() depends on this
		live_update(nullptr),
		current_speed_limit(_max_speed),
		cumulative_distance(0),
		// The motion-time accounting below reads t_prev/vinst_prev on its FIRST
		// TRAVEL, which log_entry() can reach before any section is granted -- so
		// they must start defined, not at whatever the stack held.
		vinst_prev(0), t_prev(0),
		finished(false),
		updates(TrainUpdateTrigger::UpdateType::_COUNT, nullptr)
	{
		log = new LogFile("train_log", write_train_log, true, write_log_to_screen);
		mlog = new LogFile("train_metric_log", write_metric_log, false, write_metric_to_screen);
		seclog = new LogFile("section_log", write_section_log, false, false);
	}

	Train::~Train()
	{
		// Drop every back-reference to this train before the memory goes away. An arc
		// keeps `owner` from lock() until release(), and a train that terminates while
		// still holding track is deleted by Sim::delete_terminated_objects() without
		// either being called, leaving a dangling Train* on the arc and on its segment.
		// Anything that reads ownership afterwards then dereferences freed memory:
		// MonitorTrainChartEntry does exactly that, since it starts a trace on
		// `arc->owner` whenever it sees the arc loaded, which ASan caught as a
		// heap-use-after-free in TrainChartTrace::something_changed().
		//
		// Sweeping the whole arc list rather than this train's own `arcs` and `locked`
		// is deliberate: those two are emptied along the way, so they are not a
		// complete record of what still points here, and the sweep is provably so.
		// The test is `== this`, so an arc since re-locked by another train is left
		// alone. A few hundred arcs per terminating train is nothing.
		if (network != nullptr) {
			for (auto arc : network->arcs) {
				if (arc != nullptr && arc->owner == this)
					arc->owner = nullptr;
			}
			for (auto seg : network->segments) {
				if (seg != nullptr && seg->owner == this)
					seg->owner = nullptr;
			}
		}

		for (int i = 0; i < updates.size(); i++)
			delete updates[i];
		updates.clear();

		delete_cars();

		if (log != log->master)
			delete log;
		if (mlog != mlog->master)
			delete mlog;
		// seclog was missing here, and it is allocated for every train whether section logging is on
		// or not: about 2.8 kB a train never released. One replicate is unaffected, but cpu_time_exp
		// runs a hundred in one process and 21 million completed trains would have needed 60 GB.
		if (seclog != seclog->master)
			delete seclog;
	}

	void Train::add_car(RollingStock* car) {
		cars.push_back(car);
		length += car->length;
	}//Train::add_car

	void Train::delete_cars() {
		for (int i = 0; i < cars.size(); i++)
			delete cars[i];
		cars.clear();
		length = 0;
	}

	void Train::place(DirectedSegment* arc, double _front_pos)
	{
		TrackSegment* s = arc->segment;
		arcs.push_back(arc);
		arc->lock(this);
		arc->segment->update_state(TrackSegment::LOADED, this);
		front_pos = _front_pos;
		back_pos = front_pos + length;
		assert(back_pos >= 0);
		forward = true;
		v_inst = 0;
		current_acceleration = 0;
	}

	void Train::assign(Job* _job)
	{
		if (job != nullptr) delete job;
		job = _job;
		wakeup();
	}

	void Train::notify_section_granted(TrackSection* _new_section)
	{
		assert(new_section == nullptr);
		new_section = _new_section;
		if (updates[TrainUpdateTrigger::SECTION_GRANTED] == nullptr)
			wakeup();
		else
			updates[TrainUpdateTrigger::SECTION_GRANTED]->activate_trigger();	//this will wakeup the train process
	}
	
	SimCoroutine Train::run() {
		init_log();
		log_entry();

		section = nullptr;
		new_section = nullptr;
		signals_mgr->notify_active_train(this);
		finished = false;

		while (true) {
			mlog_update(REQJOB);

			if (manager != nullptr)
				manager->ready_signal(this);

			//co_await wait_until([this]() { return job != 0; });
			if (job == nullptr)
				co_await sleep();

			mlog_update(GETJOB);
			for (int j = 0; j < job->tasks.size(); j++) {
				Task* task = job->tasks[j];
				task_idx = j;
				target = task->terminal;
				maybe_reverse();

				while (true) {	//loop through requesting and transiting sections until target is reached
					//if (id == 5)
						//int debug = 1;
					mlog_update(REQSEC);
					if (new_section == nullptr) {
						if (updates[TrainUpdateTrigger::SECTION_GRANTED] == nullptr) {
							new_section = signals_mgr->request_access(this, target, section == nullptr ? arcs.front() : section->arcs.back());
							if (new_section == nullptr)
								co_await sleep();		//sleep until a section becomes available and is assigned by the signals manager
						}
						else {
							//section access was previously requested prior to decelerating to stop but not granted yet
							co_await sleep();
							live_update->terminate();
							live_update = nullptr;
						}
					}
					mlog_update(GETSEC);

					//if (id == 5)
						//int debug = 1;

					section_log_grant(new_section);
					section = new_section;
					new_section = nullptr;
					for (auto arc : section->arcs)
						locked.push_back(arc);

					double request_next_section_t = get_speed_profile();
					double t_prev = sim.time_now;
					double t_start = sim.time_now;

					// Arm the deceleration-point trigger only if we will actually need
					// to request a follow-up section. Two cases:
					//   (a) the current section doesn't end at the task target, so
					//       another section is required to reach it;
					//   (b) the task is non-stopping AND there is a successor task to
					//       advance toward (otherwise there is nothing to plan for —
					//       the train will exit the per-task loop on arrival at
					//       arcs.front()->terminal == task->terminal below).
					// The successor-existence guard prevents an overrun at the
					// retarget step (line for job->tasks[j + 1]) when the final task
					// has stop=false (e.g. dwell=0 despawn at end of row).
					bool has_next_task = j + 1 < (int)job->tasks.size();
					if (section->terminal != task->terminal || (!task->stop && has_next_task))
						set_update_trigger(TrainUpdateTrigger::REQUEST_SECTION_PT, request_next_section_t);

					//if (eq(v_inst,0,eps) && front_pos == 0 && arcs.front()->head == section->arcs.front()->tail) {
						//special case where train is already up against the start node of the section.
					if (arcs.front() != speed_profile[0].arc) {
						//special case where train is already up against the node (numerical issue probably)
						prev_front_arc = arcs.front();
						arcs.push_front(locked.front());
						locked.pop_front();
						front_pos = arcs.front()->segment->length;
						arcs.front()->segment->update_state(TrackSegment::LOADED, this);
						section_log_front_advanced();
						//This advances the front arc, so it owes the same persistent-limit pickup the
						//FRONT_NODE branch does below. get_speed_profile() has already run, so THIS
						//profile is right either way (process_arc applies the arc's own declaration to
						//its local background); what was lost was current_speed_limit, which seeds
						//`background` on the NEXT rebuild. Without this the train kept carrying the
						//limit from before the arc it is now standing on, for the rest of the route or
						//until something else declared -- 27 trains ran a 60 km/h arc's downstream at
						//50 in inner Brisbane before this was found.
						if (arcs.front()->is_speed_change && !arcs.front()->is_transient)
							current_speed_limit = arcs.front()->speed_limit;
						log_entry();
					}

					//loop through each arc in the speed profile
					bool end_speed_profile = false;
					for (int i = 0; i < speed_profile.size(); i++) {
						SpeedProfileArc& sp_arc = speed_profile[i];
						sp_arc_ptr = &sp_arc;
						persistent_limit_now = sp_arc.persistent_limit;
						transient_limit_now = sp_arc.transient_limit;

						//a continuation span ends at an internal split point, not a real node -- fire
						//PROFILE_SPLIT there instead of FRONT_NODE, and don't touch arcs/locked when it does.
						TrainUpdateTrigger::UpdateType arrival_trigger = TrainUpdateTrigger::FRONT_NODE;
						double end_pos = eq(sp_arc.v1, 0, eps) ? sp_arc.arc->margin : 0;
						if (sp_arc.continues_in_next) {
							end_pos = sp_arc.pos_end;
							arrival_trigger = TrainUpdateTrigger::PROFILE_SPLIT;
						}
						double t = sp_arc.travel_time(front_pos, end_pos);
						set_update_trigger(arrival_trigger, t);

						// The tail can only clear during this leg if it does so at or before
						// the nose position the leg ENDS at -- `end_pos` is the signal margin
						// when the train stops here (v1 == 0), so a longer consist legitimately
						// comes to a stand still spanning the arc behind it and must keep it.
						if (lt(back_pos, front_pos, eps) && ge(front_pos - back_pos, end_pos, eps)) {
							t = sp_arc.travel_time(front_pos, front_pos - back_pos);
							set_update_trigger(TrainUpdateTrigger::BACK_NODE, t);
						}//if

						double accel_change_pos = front_pos;
						if (updates[TrainUpdateTrigger::ACCEL_CHANGE] == nullptr)
							set_update_trigger(TrainUpdateTrigger::ACCEL_CHANGE, 0);	//force an update of current_acceleration if needed

						while (true) {
							//schedule future events on this arc (not including section requests / grants)
							if (updates[TrainUpdateTrigger::ACCEL_CHANGE] == nullptr) {
								accel_change_pos = sp_arc.get_next_accel_change_pos(front_pos);
								if (gt(accel_change_pos, 0, eps)) {
									t = sp_arc.travel_time(front_pos, accel_change_pos);
									set_update_trigger(TrainUpdateTrigger::ACCEL_CHANGE, t);
								}
							}
							double pos_prev = front_pos;

							update_motion();
							log_entry();

							//if (id == 5)
								//int debug = 1;

							co_await sleep();	//sleep until the next state change happens

							//if (id == 5)
								//int debug = 1;

							if (live_update->update_type == TrainUpdateTrigger::BACK_NODE) {

								arcs.back()->delayed_release(this);
								arcs.pop_back();
								front_pos -= back_pos;
								back_pos = arcs.back()->segment->length;
								if (lt(back_pos, front_pos, eps) && ge(front_pos - back_pos, end_pos, eps)) {
									t = sp_arc.travel_time(front_pos, front_pos - back_pos);
									set_update_trigger(TrainUpdateTrigger::BACK_NODE, t);
								}
							}
							else if (live_update->update_type == TrainUpdateTrigger::PROFILE_SPLIT) {
								//an internal split point within the SAME physical arc (a transient speed
								//limit's clearance zone). No node is crossed, so arcs/locked/
								//current_speed_limit are untouched and the outer for-loop's i++ (below, via
								//the same break this shares with FRONT_NODE) moves sp_arc_ptr on. The
								//train HAS moved, though, to a position this trigger knows exactly, so
								//record it the way ACCEL_CHANGE does -- otherwise front_pos stays stale on
								//the continuation span (whose pos_start is this very point) and the
								//cumulative_distance accrual below silently books zero for the span.
								back_pos -= front_pos - sp_arc.pos_end;
								front_pos = sp_arc.pos_end;
							}
							else if (live_update->update_type == TrainUpdateTrigger::FRONT_NODE) {
								if (i < speed_profile.size() - 1) {
									back_pos -= front_pos;
									front_pos = 0;
									prev_front_arc = arcs.front();
									arcs.push_front(locked.front());
									locked.pop_front();
									front_pos = arcs.front()->segment->length;
									arcs.front()->segment->update_state(TrackSegment::LOADED, this);
									section_log_front_advanced();
									if (arcs.front()->is_speed_change && !arcs.front()->is_transient)
										current_speed_limit = arcs.front()->speed_limit;
								}
								else {
									//stopped at signal (potentially before reaching the head node)
									cancel_update_trigger(TrainUpdateTrigger::BACK_NODE);
									back_pos -= (front_pos - sp_arc.arc->margin);
									front_pos = sp_arc.arc->margin;
								}//else
							}
							else if (live_update->update_type == TrainUpdateTrigger::REQUEST_SECTION_PT) {
								double time_delta = sim.time_now - t_prev;
								double position_delta = v_inst * time_delta + 0.5 * current_acceleration * time_delta * time_delta;
								front_pos -= position_delta;
								back_pos -= position_delta;
								assert(section != nullptr && new_section == nullptr);
								Terminal* request_target = target;
								// Passing through the task terminal without stopping — retarget the
								// next section toward the *next* task's terminal. Guard against
								// j+1 overrunning the task vector: the trigger-arming logic above
								// already excludes this case when has_next_task is false, but the
								// guard keeps us safe against any future code path that arms the
								// trigger and reaches here on the final task.
								if (arcs.front()->terminal == task->terminal || (locked.size() > 0 && locked.back()->terminal == task->terminal)) {
									if (j + 1 < (int)job->tasks.size())
										request_target = job->tasks[j + 1]->terminal;
								}
								// Decide BEFORE the request. A routing policy may retarget on the grant
								// (FlexiblePlatformSignalsManager::on_granted rewrites `target`), so
								// comparing `request_target != target` afterwards would read a platform
								// substitution as passing through, advance past the call and skip its dwell.
								bool advancing = request_target != target;
								new_section = signals_mgr->request_access(this, request_target, section->arcs.back());
								if (new_section == nullptr)
									set_update_trigger(TrainUpdateTrigger::SECTION_GRANTED);
								else {
									end_speed_profile = true;
									cancel_motion_triggers();
									if (advancing) {
										task = job->tasks[++j];
										task_idx = j;
										target = task->terminal;
									}
								}
							}
							else if (live_update->update_type == TrainUpdateTrigger::SECTION_GRANTED) {
								double time_delta = sim.time_now - t_prev;
								double position_delta = v_inst * time_delta + 0.5 * current_acceleration * time_delta * time_delta;
								front_pos -= position_delta;
								back_pos -= position_delta;
								end_speed_profile = true;
								cancel_motion_triggers();

								if (arcs.front()->terminal == task->terminal || (locked.size() > 0 && locked.back()->terminal == task->terminal)) {
									//handles the case of reaching zero dwell terminal and passing at speed to next target
									task = job->tasks[++j];
									task_idx = j;
									target = task->terminal;
								}
							}
							else if (live_update->update_type == TrainUpdateTrigger::ACCEL_CHANGE) {
								back_pos -= front_pos - accel_change_pos;
								front_pos = accel_change_pos;
								if (gt(front_pos, sp_arc.pos_coast, eps))
									current_acceleration = accel;
								else if (gt(front_pos, sp_arc.pos_decel, eps))
									current_acceleration = 0;
								else
									current_acceleration = -decel;
							}
							else {
								// Dispatch above covers FRONT_NODE, PROFILE_SPLIT, BACK_NODE, ACCEL_CHANGE,
								// REQUEST_SECTION_PT, SECTION_GRANTED. Any other update_type
								// here is a logic error (e.g. SECTION_END being set as
								// live_update, or a new TrainUpdateTrigger::UpdateType added
								// without extending this dispatch). Record rich context for
								// post-mortem and abort this one train's coroutine rather
								// than silently falling through with undefined physics.
								const char* ut_name = "<unknown>";
								switch (live_update->update_type) {
									case TrainUpdateTrigger::FRONT_NODE:         ut_name = "FRONT_NODE"; break;
									case TrainUpdateTrigger::BACK_NODE:          ut_name = "BACK_NODE"; break;
									case TrainUpdateTrigger::ACCEL_CHANGE:       ut_name = "ACCEL_CHANGE"; break;
									case TrainUpdateTrigger::REQUEST_SECTION_PT: ut_name = "REQUEST_SECTION_PT"; break;
									case TrainUpdateTrigger::SECTION_GRANTED:    ut_name = "SECTION_GRANTED"; break;
									case TrainUpdateTrigger::SECTION_END:        ut_name = "SECTION_END"; break;
									case TrainUpdateTrigger::PROFILE_SPLIT:      ut_name = "PROFILE_SPLIT"; break;
									default: break;
								}
								string msg = string("unhandled update_type ") + ut_name
									+ " (" + to_string(static_cast<int>(live_update->update_type))
									+ ") in speed-profile dispatch; expected FRONT_NODE / BACK_NODE / "
									+ "ACCEL_CHANGE / REQUEST_SECTION_PT / SECTION_GRANTED. Aborting train "
									+ to_string(id) + " at sim_time=" + to_string(sim.time_now) + ".";
								network->record_warning(this, arcs.empty() ? nullptr : arcs.front(), msg);
								cerr << "[Train " << id << "] " << msg << endl;
								if (live_update) {
									live_update->terminate();
									live_update = nullptr;
								}
								co_return;
							}

							t_prev = sim.time_now;
							cumulative_distance += pos_prev - (live_update->update_type == TrainUpdateTrigger::FRONT_NODE ? 0 : front_pos);

							if (live_update->update_type == TrainUpdateTrigger::FRONT_NODE
								|| live_update->update_type == TrainUpdateTrigger::PROFILE_SPLIT
								|| end_speed_profile) {
								live_update->terminate();
								live_update = nullptr;
								if (arcs.front() == sp_arc.arc) {
									update_motion();
									log_entry();
								}
								break;
							}
							live_update->terminate();
							live_update = nullptr;
						}//wend

						if (end_speed_profile)
							break;	//if here, we are exiting the speed profile early because a new section has been granted

					}//i / sp_arc
					update_motion();
					if (arcs.front()->terminal == task->terminal) {
						// Stand for the minimum dwell, or until the booked departure if
						// the train got here early -- whichever is longer. A late train
						// is held by nothing, since its slot is already behind it.
						double stand = task->dwell_time;
						if (task->stop && ge(task->earliest_departure, 0.0, eps)) {
							double to_slot = task->earliest_departure - sim.time_now;
							if (gt(to_slot, stand, eps))
								stand = to_slot;
						}
						if (gt(stand, 0.0, eps) && task->stop) {
							mlog_update(STDWL);
							co_await delay(stand);
							mlog_update(ENDDWL);
						}
						break;
					}
						
				}//wend (loop through requesting/transiting sections until target reached)

			}//task
			mlog_update(ENDJOB);

			delete job;
			job = nullptr;
			task_idx = -1;
			if (!persistent)
				break;
		}//wend
		
		signals_mgr->notify_inactive_train(this);
		for (auto arc : arcs)
			arc->release(this);
		finished = true;
		signals_mgr->notify_segment_free();

		//if (sim.time_now > 600)
		//	int debug = 1;

		co_await delay(1);	//allow other related processes time to cleanup

		terminate();
		//delete this;
		co_return;
	}//Train::run

	//returns the time delay after which the next section should be requested
	double Train::get_speed_profile()
	{
		//determine required speed changes across the remainder of the current section, then the new section
		//
		struct SpeedChanges {
			double d;		//distance from starting location of speed profile
			double length;	//length of ruling speed limit or arc
			double s;		//speed limit imposed from start of speed change
			DirectedSegment* arc;	//arc on which the speed change will occur

			double v0;		//speed at start of speed change or arc
			double v1;		//speed at start of next speed change or arc
			double t0;		//time since start of speed profile reaching start of arc
			double t1;		//time since start of speed profile reaching end of arc

			SpeedChanges(double _d, double _s, DirectedSegment* _arc) : d(_d), s(_s), arc(_arc) {}
		};

		//one entry per PORTION of an arc appearing in this profile -- usually the whole arc, but
		//a transient speed limit's tail-clearance zone can split one physical arc into two spans
		//(see continues_in_next on SpeedProfileArc: Train::run must not wait for a FRONT_NODE
		//event between two spans of the same arc, because there is no real node there).
		struct ArcSpan {
			DirectedSegment* arc;
			double len;
			bool split;		//true iff the NEXT span is another portion of this same arc (a transient
								//clearance point falls strictly inside the arc). Exact by construction:
								//a span is only marked when its continuation is about to be pushed.
			double persistent;	//the prevailing limit carried into this span
			double transient;	//whatever restricts below it here, 0 for none
			ArcSpan(DirectedSegment* _arc, double _len, bool _split, double _persistent, double _transient)
				: arc(_arc), len(_len), split(_split), persistent(_persistent), transient(_transient) {}
		};

		vector<SpeedChanges> speed_limit_changes;
		vector<ArcSpan> arc_seq;

		//Active transient restrictions over this profile, as (clear point in THIS profile's
		//d-frame, ceiling). Seeded from transient_pending, which is kept in cumulative_distance's
		//frame precisely so a clearance zone that spans a section boundary just keeps working with
		//no bookkeeping at the grant site. A transient only ever RESTRICTS: the ruling limit at any
		//point is the background limit capped by every transient still in force there.
		vector<pair<double, double>> active;
		for (auto& pending : transient_pending) {
			if (lt(cumulative_distance, pending.first, eps))
				active.emplace_back(pending.first - cumulative_distance, pending.second);
		}

		double background = current_speed_limit;	//ruling PERSISTENT limit, updated as arcs are passed
		double d = 0;

		auto ruling_limit = [&](double at_d) {
			double s = background;
			for (auto& t : active) {
				if (gt(t.first, at_d, eps))
					s = min(s, t.second);
			}
			return s;
		};

		auto open_zone = [&](DirectedSegment* arc) {
			if (!speed_limit_changes.empty())
				speed_limit_changes.back().length = d - speed_limit_changes.back().d;
			speed_limit_changes.emplace_back(d, ruling_limit(d), arc);
		};

		//process one arc's worth of remaining distance at a time, uniformly for the currently
		//partially-traversed front arc (len = front_pos) and every subsequently locked arc
		//(len = full segment length).
		//The profile ends at the LAST arc's signal margin, not at its head node (see the
		//`d -= margin` below), so a clearance point falling in that last `margin` of the final
		//arc is beyond anywhere the train ever gets. Splitting there produced a final span
		//running BACKWARDS -- observed start_pos 0.009 against end_pos 0.010, i.e. -1 m -- and
		//set_phases then left has_accel, has_coast and has_decel ALL false, so the next
		//get_inst_speed tripped assert(has_decel) and the run aborted. Only reachable with a
		//dense speed-limit set, where a clearance point is far more likely to land in the last
		//few metres of an arc, which is why the sparse hand-built limits never hit it.
		//
		//Set to the final arc's margin while that arc is walked, 0 otherwise. Note it deliberately
		//does NOT change how `d` advances or how `active` is seeded: a transient clears when the
		//train's TAIL passes the real arc, which has nothing to do with where the front stops.
		double split_limit = 0.0;

		auto process_arc = [&](DirectedSegment* arc, double len) {
			//A clearance ending exactly at this arc's start is not an interior split point of the
			//previous arc, so its reversion is emitted here instead.
			bool cleared_at_start = false;
			for (auto& t : active) {
				if (eq(t.first, d, eps))
					cleared_at_start = true;
			}

			if (arc->is_speed_change) {
				if (arc->is_transient) {
					//applies until the REAR of the train has cleared the arc: this arc's own remaining
					//length plus the whole train. Overlapping transients simply coexist in `active`,
					//each expiring on its own clearance point, tightest in force winning throughout.
					active.emplace_back(d + len + this->length, arc->speed_limit);
				}
				else {
					//A prevailing limit is asymmetric about where it takes effect. Where it DROPS it
					//binds at the nose -- the train has to be down to the lower speed as the front
					//enters the zone. Where it RISES the train is LEAVING a zone whose tail it is
					//still inside, so the limit it is leaving has to hold until the whole consist is
					//clear of the boundary; releasing it at the nose lets a train accelerate with most
					//of itself still on the curve or through the turnout that justified the
					//restriction. The clearance point is the boundary (`d`, the arc's entry node) plus
					//the train's length -- the same arithmetic the transient branch above uses, applied
					//to the zone being left rather than the one being entered. Parking it in `active`
					//means the existing machinery does the rest: `ruling_limit` keeps taking the
					//minimum, the walk below splits whichever arc the clearance lands inside, and
					//`transient_pending` carries it across a profile rebuild.
					if (gt(arc->speed_limit, background, eps))
						active.emplace_back(d + this->length, background);
					background = arc->speed_limit;
				}
				//A declaration that does not change the ruling speed must not open a zone. The
				//backward sweep caps each zone at the fastest it can be and still meet the NEXT
				//zone's speed by the boundary, so a zone boundary is a POINT constraint. Splitting
				//a braking envelope in two therefore forces the train down to the second half's
				//ceiling at the boundary, when unsplit it could have run faster for longer and
				//braked once. Both plans stop in the same place; the split one is slower for no
				//physical reason.
				//
				//Measured 2026-09-15: declaring 100 on an arc already carrying 100 cost 0.53 s on
				//one traversal, and 282 such redundant declarations cost 6.9 s across 130 trains.
				//Nothing warns, because the instantaneous-deceleration fallback only fires when a
				//cap cannot be met, and these can always be met.
				//
				//This does NOT fix the underlying approximation: a GENUINE limit change landing
				//inside a braking envelope still gets the conservative plan. That is the documented
				//"assumes sufficient distance between speed limit changes" limitation and needs a
				//real multi-zone envelope to fix. What this removes is the case where the input
				//says nothing new at all.
				if (!eq(ruling_limit(d), speed_limit_changes.empty()
				        ? -1.0 : speed_limit_changes.back().s, eps))
					open_zone(arc);
			}
			else if (speed_limit_changes.empty() || cleared_at_start)
				open_zone(arc);		//seed the profile's first zone, or revert on a clearance ending here

			//walk the arc, breaking it at every clearance point strictly inside it
			double remaining = len;
			while (true) {
				double next = -1;
				for (auto& t : active) {
					if (gt(t.first, d, eps) && lt(t.first, d + remaining - split_limit, eps) && (lt(next, 0, eps) || lt(t.first, next, eps)))
						next = t.first;
				}
				if (lt(next, 0, eps))
					break;
				double part = next - d;
				arc_seq.emplace_back(arc, part, true,     //more of this same arc follows
					background, lt(ruling_limit(d), background, eps) ? ruling_limit(d) : 0.0);
				d += part;
				remaining -= part;
				open_zone(arc);							//a transient just expired at this point
			}
			arc_seq.emplace_back(arc, remaining, false,
				background, lt(ruling_limit(d), background, eps) ? ruling_limit(d) : 0.0);
			d += remaining;
		};

		vector<pair<DirectedSegment*, double>> walk;
		if (gt(front_pos, 0, eps))
			walk.emplace_back(arcs.front(), front_pos);
		for (auto arc : locked)
			walk.emplace_back(arc, arc->segment->length);
		for (size_t wi = 0; wi < walk.size(); ++wi) {
			split_limit = (wi + 1 == walk.size()) ? walk[wi].first->margin : 0.0;
			process_arc(walk[wi].first, walk[wi].second);
		}
		split_limit = 0.0;

		//republish whatever is still in force at the end of this profile, back in the absolute
		//cumulative_distance frame, so the NEXT get_speed_profile() call picks it up. Entries whose
		//clearance point has already passed are dropped here, which is what keeps the list short.
		transient_pending.clear();
		for (auto& t : active) {
			if (gt(t.first, 0.0, eps))
				transient_pending.emplace_back(cumulative_distance + t.first, t.second);
		}

		//assume worst case that train has to stop at signal
		d -= section->arcs.back()->margin;
		speed_limit_changes.back().length = d - speed_limit_changes.back().d;
		speed_limit_changes.emplace_back(d, 0, section->arcs.back());
		speed_limit_changes.back().length = 0;	//once train stops, it doesn't move further

		//now work out the speed profile
		speed_profile.clear();
		speed_profile.resize(arc_seq.size());
		d = 0;
		int seq = 0;
		for (auto& span : arc_seq) {		//initialise speed profile broken down by arc spans
			//pos_start/pos_end are always "distance to the ARC'S head node", regardless of which
			//span of that arc this is. A fresh arc starts at its full length (or front_pos, for the
			//very first span in the profile, since the train is already partway into it); a
			//continuation span (the second half of a same-arc split) picks up exactly where the
			//preceding span of the SAME arc left off.
			bool continues_prev = seq > 0 && arc_seq[seq - 1].split;
			double start_pos = continues_prev
				? speed_profile[seq - 1].pos_end
				: (seq == 0 && gt(front_pos, 0, eps) ? front_pos : span.arc->segment->length);
			double end_pos = (seq == (int)arc_seq.size() - 1) ? span.arc->margin : start_pos - span.len;
			//A span must have positive length or set_phases leaves every phase flag false and
			//get_inst_speed has nothing to evaluate. Should be unreachable now that the walk
			//refuses to split the final arc past the stopping point, so say so loudly rather
			//than let a future regression go quiet.
			if (!gt(start_pos - end_pos, 0.0, eps))
				network->record_warning(this, span.arc, "degenerate speed profile span");
			speed_profile[seq].set_parameters(this, span.arc, seq, d, start_pos, end_pos);
			speed_profile[seq].continues_in_next = span.split;
			speed_profile[seq].persistent_limit = span.persistent;
			speed_profile[seq].transient_limit = span.transient;
			d += speed_profile[seq].length;
			++seq;
		}//arc

		//A deceleration has to fit inside the span it begins in. Where the limit ahead is low
		//enough that braking would have to start upstream, lower THIS span's ruling speed to the
		//fastest it can be and still reach the next limit by the span boundary, and keep walking
		//back so the reduction propagates as far upstream as it needs to. Without this the forward
		//pass below produces a negative coast distance -- pos_decel lands beyond pos_start and
		//t_coast goes negative -- and the train then holds the higher speed right to the span
		//boundary before dropping to the correct speed instantaneously, arriving early.
		for (int i = (int)speed_limit_changes.size() - 2; i >= 0; --i) {
			SpeedChanges& sl = speed_limit_changes[i];
			double v_cap = ssqrt(speed_limit_changes[i + 1].s * speed_limit_changes[i + 1].s
				+ 2 * decel * sl.length, eps);
			if (lt(v_cap, sl.s, eps))
				sl.s = v_cap;
		}

		double s = v_inst; //start with current speed
		int aseq = 0;
		double t = 0;
		double v_max;

		for (int sc_seq = 0; sc_seq < speed_limit_changes.size() - 1; sc_seq++) {
			SpeedChanges& sl0 = speed_limit_changes[sc_seq];
			SpeedChanges& sl1 = speed_limit_changes[sc_seq + 1];
			sl0.t0 = t;
			if (lt(sl0.s, s, eps)) {
				//we have a slight problem, train did not have time to decelerate, force instantaneous speed change
				network->record_warning(this, sl0.arc, "instantaneous deceleration required");
				s = sl0.s;
			}
			sl0.v0 = s;
			
			bool is_accel = gt(sl0.s, s, eps);		//true if speed limit change starts with acceleration
			bool is_decel = gt(s, sl1.s, eps) || gt(sl0.s, sl1.s, eps);	//true if speed limit change ends with deceleration
			double accel_d = 0;
			double accel_t = 0;
			double decel_d = sl0.length;
			double decel_t = 0;
			v_max = 0;
			if (is_accel) {
				//calculate total acceleration distance
				v_max = sl0.s;
				accel_t = (sl0.s - sl0.v0) / accel;
				accel_d = sl0.v0 * accel_t + 0.5 * accel * accel_t * accel_t;
			}
			if (is_decel) {
				v_max = sl0.s;
				decel_t = (sl0.s - sl1.s) / decel;
				decel_d = sl0.length - (sl0.s * decel_t - 0.5 * decel * decel_t * decel_t);
				sl0.v1 = sl1.s;
			}

			if (is_accel && is_decel && gt(accel_d, decel_d, eps)) {
				//this is the more difficult case where acceleration phase must be truncated to start decelerating in time
				v_max = ssqrt((accel * sl0.v1 * sl0.v1 + decel * sl0.v0 * sl0.v0 + 2 * accel * decel * sl0.length) / (accel + decel), eps);
				accel_t = (v_max - sl0.v0) / accel;
				accel_d = sl0.v0 * accel_t + 0.5 * accel * accel_t * accel_t;
				decel_d = accel_d;
			}

			SpeedProfileArc* sp = &speed_profile[aseq];
			bool accel_done = !is_accel;
			bool decel_started = false;
			while (le(sp->d + sp->length, sl0.d + sl0.length, eps)) {
				if (!accel_done) {
					if (le(sp->d + sp->length, sl0.d + accel_d, eps)) {
						//case 1: accelerating whole way through the arc
						sp->set_phases(s, t, sp->length, 0, 0);
						if (eq(sp->d + sp->length, sl0.d + accel_d, eps))
							accel_done = true;
					}
					else if (le(sp->d + sp->length, sl0.d + decel_d)) {
						//case 2: stop accelerating part way through the arc, then coast
						double d_part1 = sl0.d + accel_d - sp->d;		//acceleration
						double d_part2 = sp->length - d_part1;			//coasting
						sp->set_phases(s, t, d_part1, d_part2, 0);
						accel_done = true;
					}
					else {
						//case 3: stop accelerating part way, possibly coast, then decelerate rest of the way
						double d_part1 = sl0.d + accel_d - sp->d;					//acceleration
						double d_part2 = decel_d - accel_d;							//coasting
						double d_part3 = sp->length - (d_part1 + d_part2);			//deceleration		
						sp->set_phases(s, t, d_part1, d_part2, d_part3);
						accel_done = true;
						decel_started = true;
					}
				}//if
				else if (!decel_started) {
					if (le(sp->d + sp->length, sl0.d + decel_d, eps)) {
						//case 4: coasting all the way
						sp->set_phases(s, t, 0, sp->length, 0);
						if (eq(sp->d + sp->length, sl0.d + decel_d, eps))
							decel_started = true;
					}
					else {
						//case 5: coasting, then decelerate rest of the way
						double d_part1 = sl0.d + decel_d - sp->d;
						double d_part2 = sp->length - d_part1;
						sp->set_phases(s, t, 0, d_part1, d_part2);
						decel_started = true;
					}
				}
				else
					//case 6: decelerating all the way
					sp->set_phases(s, t, 0, 0, sp->length);

				t += sp->t;
				s = sp->v1;
				if (aseq < speed_profile.size() - 1)
					sp = &speed_profile[++aseq];
				else
					break;
			}//wend(aseq)

			sl0.v1 = s;
			sl0.t1 = t;
		}//speed_chg
		SpeedChanges& end_sl = speed_limit_changes[speed_limit_changes.size() - 2];
		double section_request_t = end_sl.t1 - v_max / decel;
		if (lt(section_request_t, 0, eps)) {
			network->record_warning(this, end_sl.arc, "not enough time to decelerate to stop");
			return 0;
		}
		return section_request_t;	//return maximum time to decelerate to stop (point to request next section)
	}

	double Train::get_transit_time(double v0, double a, double d)
	{
		if (eq(a, 0.0, eps)) return d / v0;
		return (-v0 + sqrt(v0*v0 + 2*a*d))/a;
	}

	void Train::set_update_trigger(TrainUpdateTrigger::UpdateType update_type, double delay_time)
	{
		if (updates[update_type] != nullptr)
			cancel_update_trigger(update_type);
		updates[update_type] = new TrainUpdateTrigger(this, update_type, delay_time);
	}

	void Train::cancel_motion_triggers()
	{
		cancel_update_trigger(TrainUpdateTrigger::FRONT_NODE);
		cancel_update_trigger(TrainUpdateTrigger::BACK_NODE);
		cancel_update_trigger(TrainUpdateTrigger::ACCEL_CHANGE);
		cancel_update_trigger(TrainUpdateTrigger::PROFILE_SPLIT);
	}

	void Train::cancel_update_trigger(TrainUpdateTrigger::UpdateType update_type)
	{
		if (updates[update_type] != nullptr) {
			updates[update_type]->cancel_trigger = true;
			updates[update_type] = nullptr;
		}
	}

	void Train::update_motion()
	{
		if (gt(front_pos, sp_arc_ptr->pos_coast, eps))
			current_acceleration = accel;
		else if (gt(front_pos, sp_arc_ptr->pos_decel, eps))
			current_acceleration = 0;
		else
			current_acceleration = -decel;
		v_inst = sp_arc_ptr->get_inst_speed(front_pos);
	}

	void Train::action_update_trigger(TrainUpdateTrigger* update_trigger)
	{
		//if (sim.time_now > 3.27834029095164 && id == 9)
		//	int debug = 1;

		TrainUpdateTrigger::UpdateType update_type = update_trigger->update_type;
		assert(live_update == nullptr && updates[update_type] == update_trigger);
		live_update = update_trigger;
		updates[update_type] = nullptr;
		wakeup();
	}

	void Train::init_log()
	{
		mlog->add_static_field("id", to_string(id));
		mlog->add("ready_time", "double");
		mlog->add("completion_time", "double");
		mlog->add("start_loc", "string");
		mlog->add("trip_seq", "int");      // 1,2,... per train (a persistent train chains many trips)
		mlog->add("job_id", "int");        // which DB job definition this trip executed
		mlog->add("service", "string");    // spawner service_name (GTFS trip_id for the inner-Brisbane scenarios)
		mlog->add("job_list", "string");
		mlog->add("wait_for_trip", "double");
		mlog->add("job_to_train_wait", "double");   // sim time between job creation and its assignment to this train
		mlog->add("total_time", "double");
		mlog->add("total_dwell_time", "double");
		mlog->add("total_travel_time", "double");
		mlog->add("total_wait_time", "double");
		mlog->add("total_accel_time", "double");
		mlog->add("total_coast_time", "double");
		mlog->add("total_decel_time", "double");
		mlog->add("total_distance", "double");
		mlog->add("avg_speed", "double");
		// string so it can be BLANK for a persistent train's 2nd+ jobs (spawn delay
		// only applies to the initial spawn; per-trip idle is wait_for_trip).
		mlog->add("spawn_delay", "string");

		// Section log. One row per section traversed; see Train::write_section_log.
		seclog->add("train_id", "int");
		seclog->add("service", "string");
		seclog->add("section_id", "int");
		seclog->add("entry_time", "double");
		seclog->add("exit_time", "double");        // front reaches the head node of the section's last arc
		seclog->add("stop_time", "double");      // -1 when the train did not stop in this section
		seclog->add("stop_duration", "double");
		seclog->add("next_grant_time", "double");  // when the FOLLOWING section was granted, -1 if never
		seclog->add("terminal", "string");       // blank unless it stood at a terminal here

		if (mlog_master == nullptr)
			mlog_master = mlog->master;

		if (!write_train_log)
			return;
		log->add_static_field("id", to_string(id));
		log->add_static_field("length", to_string(length));
		log->add("target", "string");
		log->add("direction", "int");
		log->add("speed", "double");
		log->add("acceleration", "double");
		log->add("front_arc", "str");
		log->add("f_to_head", "double");
		log->add("f_from_tail", "double");
		log->add("terminal", "string");
		log->add("back_arc", "str");
		log->add("b_to_head", "double");
		log->add("b_from_tail", "double");
		log->add("tot_dist", "double");
		log->add("occupies", "string");
		log->add("locked", "string");
		//Appended at the END so anything reading this file by column position is unaffected;
		//every consumer in the repo uses a header-keyed reader, but the archive holds older
		//runs and the file is published with the paper.
		log->add("ruling_limit", "double");
		log->add("transient_limit", "string");	//string so "no transient" can be blank
	}//init_log

	void Train::log_entry()
	{
		mlog_update(TRAVEL);

		if (!write_train_log)
			return;
		log->data("target", target ? target->name : "");
		log->data("direction", forward);
		log->data("speed", v_inst);
		log->data("acceleration", current_acceleration);
		log->data("front_arc", arcs.front()->str_id);
		log->data("f_to_head", front_pos);
		log->data("f_from_tail", arcs.front()->segment->length - front_pos);
		log->data("terminal", (arcs.front()->is_terminal && arcs.front()->terminal) ? arcs.front()->terminal->name : "");
		log->data("back_arc", arcs.back()->str_id);
		log->data("b_to_head", back_pos);
		log->data("b_from_tail", arcs.back()->segment->length - back_pos);
		log->data("tot_dist", cumulative_distance);
		//What limit is this train actually under right now, and is it the prevailing one?
		//Reconstructing this from the arc's own speed_limit is wrong wherever a limit is
		//carried across arcs that declare none, which is most of a throat.
		log->data("ruling_limit", gt(persistent_limit_now, 0.0, eps) ? persistent_limit_now
																	  : current_speed_limit);
		log->data("transient_limit", gt(transient_limit_now, 0.0, eps)
									 ? to_string(transient_limit_now) : "");
		log->data("occupies", occupancy_str());
		log->data("locked", occupancy_str(true));
		log->write_current_record();

	}

	void Train::section_log_enter(TrackSection* sec)
	{
		sec_cur = sec;
		sec_entry_t = sim.time_now;
		sec_grant_t = -1;
		sec_stop_t = -1;
		sec_stop_dur = 0;
		sec_terminal = nullptr;
		sec_last_arc = (sec && !sec->arcs.empty()) ? sec->arcs.back() : nullptr;
	}

	void Train::section_log_grant(TrackSection* granted)
	{
		// The very first grant of a job: the front is already standing in that section, so
		// this is an entry rather than a look-ahead. Every later grant is the look-ahead.
		if (sec_cur == nullptr)
			section_log_enter(granted);
		else if (lt(sec_grant_t, 0.0, eps))
			sec_grant_t = sim.time_now;
	}

	void Train::section_log_front_advanced()
	{
		// The front has just moved onto a new arc. If the arc it left was the last of the
		// current section, the head has reached that section's head node and the traversal
		// is over -- which is the physical exit, not the grant.
		if (sec_cur && sec_last_arc && !arcs.empty() && arcs.front() != sec_last_arc
			&& prev_front_arc == sec_last_arc) {
			section_log_exit();
			section_log_enter(section);
		}
	}

	void Train::section_log_exit()
	{
		if (write_section_log && sec_cur) {
			seclog->data("train_id", id);
			seclog->data("service", job ? job->service_name : string(""));
			seclog->data("section_id", sec_cur->id);
			seclog->data("entry_time", sec_entry_t);
			seclog->data("exit_time", sim.time_now);
			seclog->data("stop_time", ge(sec_stop_t, 0.0, eps) ? sec_stop_t : -1.0);
			seclog->data("stop_duration", sec_stop_dur);
			// When the FOLLOWING section was granted. Earlier than the exit whenever the train
			// ran straight through, later when it had to wait at the signal for it.
			seclog->data("next_grant_time", ge(sec_grant_t, 0.0, eps) ? sec_grant_t : -1.0);
			seclog->data("terminal", sec_terminal ? sec_terminal->name : string(""));
			seclog->write_current_record();
		}
		sec_cur = nullptr;
		sec_last_arc = nullptr;
	}

	void Train::mlog_update(LogPhase phase)
	{
		string job_list;
		double deltat, deltas;
		switch (phase) {
		case REQJOB:  //train ready for job
			mlog->field("ready_time")->timestamp();
			mlog->field("wait_for_trip")->start_delay();
			mlog->data("start_loc", arcs.front()->terminal->name);
			// spawn_delay only applies to the initial spawn -> record it on the
			// first trip only; left blank thereafter (trip_seq not yet incremented).
			if (trip_seq == 0)
				mlog->data("spawn_delay", to_string(sim.time_now - spawn_scheduled_time));
			vinst_prev = 0;
			t_prev = sim.time_now;		//belongs with the accumulators it measures against
			mlog->data("total_accel_time", 0);
			mlog->data("total_coast_time", 0);
			mlog->data("total_decel_time", 0);
			break;
		case GETJOB:	//job assigned
			mlog->field("wait_for_trip")->calc_delay();
			mlog->data("job_to_train_wait", sim.time_now - job->creation_time);
			mlog->data("trip_seq", ++trip_seq);   // 1 for the first trip, then 2,3,...
			mlog->data("job_id", job->id);
			mlog->data("service", job->service_name);
			job_list = "";
			for (auto t : job->tasks)
				job_list += "(" + t->terminal->name + "|" + to_string(t->dwell_time) + ")";
			mlog->data("job_list", job_list);
			mlog->field("total_time")->start_delay();
			mlog->field("total_travel_time")->start_delay();
			cumulative_distance = 0.0;
			transient_pending.clear();	//kept in cumulative_distance's frame, so it must be cleared
										//with it. A new job is only assigned at rest, so nothing was
										//actually mid-clearance anyway.
			break;
		case REQSEC: //request section when at rest
			mlog->field("total_wait_time")->start_delay();
			// At rest asking for the next section, so the train has stopped at the signal at
			// the margin at the end of the CURRENT section. That stop belongs to this section's
			// row, which is why it is recorded before the grant replaces it.
			if (lt(sec_stop_t, 0.0, eps))
				sec_stop_t = sim.time_now;
			break;
		case GETSEC: //section assigned when at rest
			mlog->field("total_wait_time")->calc_delay();
			// A request granted immediately is not a stop: REQSEC/GETSEC fire on every
			// section whether or not the train actually came to rest, so a zero-length
			// wait must be discarded or every section looks like it held a train.
			//
			// And skip entirely while standing at a terminal. A train asks for its next
			// section DURING the dwell, so this fires mid-dwell and would add the elapsed
			// stand on top of what ENDDWL adds -- which doubled every terminal dwell.
			if (sec_terminal == nullptr && ge(sec_stop_t, 0.0, eps)) {
				double waited = sim.time_now - sec_stop_t;
				if (gt(waited, 0.0, eps))
					sec_stop_dur += waited;
				else if (eq(sec_stop_dur, 0.0, eps))
					sec_stop_t = -1;
			}
			t_prev = sim.time_now;
			break;
		case TRAVEL: //acceleration change
			deltat = sim.time_now - t_prev;
			deltas = v_inst - vinst_prev;
			if (gt(deltas, 0.0, eps))
				mlog->field("total_accel_time")->dval += deltat;
			else if (lt(deltas, 0.0, eps))
				mlog->field("total_decel_time")->dval += deltat;
			else if (gt(v_inst, 0.0, eps))
				mlog->field("total_coast_time")->dval += deltat;
			vinst_prev = v_inst;
			t_prev = sim.time_now;
			break;
		case STDWL: //start dwell
			mlog->field("total_travel_time")->calc_delay();
			mlog->field("total_dwell_time")->start_delay();
			sec_stop_t = sim.time_now;
			sec_terminal = arcs.empty() ? nullptr : arcs.front()->terminal;
			break;
		case ENDDWL: //end dwell
			mlog->field("total_travel_time")->start_delay();
			mlog->field("total_dwell_time")->calc_delay();
			if (ge(sec_stop_t, 0.0, eps))
				sec_stop_dur += sim.time_now - sec_stop_t;
			break;
		case ENDJOB: //job complete
			section_log_exit();   // the last section has no successor to push the front out of it
			mlog->field("total_travel_time")->calc_delay();
			mlog->data("total_distance", cumulative_distance);
			mlog->field("completion_time")->timestamp();
			mlog->field("total_time")->calc_delay();
			mlog->data("avg_speed", cumulative_distance / mlog->field("total_travel_time")->dval);
			mlog->write_current_record();
			break;
		}//switch
	}

	void Train::maybe_reverse()
	{
		if (!network->automatic_reverse_detect || arcs.empty() || target == nullptr)
			return;
		if (!eq(v_inst, 0.0, eps))
			return;			//only ever turn around at a stand

		DirectedSegment* ahead = arcs.front();
		DirectedSegment* behind = arcs.back()->segment->get_arc(
			arcs.back()->segment->other_node(arcs.back()->head));
		bool fwd = network->can_reach(ahead, target);
		bool rev = network->can_reach(behind, target);

		if (fwd) {
			// A target reachable both ways is not wrong, but the forward route is
			// taken silently and may be far longer than turning around, so say so.
			if (rev)
				network->record_warning(this, ahead,
					"target " + target->name + " reachable in both directions; going forward");
			return;
		}
		if (!rev) {
			// Previously this queued forever with no output. The train still queues
			// (some other train may yet free the route), but the cause is now visible.
			network->record_warning(this, ahead,
				"target " + target->name + " unreachable in either direction");
			return;
		}
		reverse();
	}

	void Train::reverse()
	{
		// Nothing moves. The train keeps every segment it occupies and owns, so there
		// is no lock to hand back and nothing to release -- it has run to the end of
		// its section, so `locked` is empty. Warn rather than assume, because a
		// lock-through section (finalise_sections concatenates two) can put a terminal
		// mid-section and leave arcs locked ahead.
		if (!locked.empty())
			network->record_warning(this, arcs.front(),
				"reversal with " + to_string(locked.size()) + " arcs still locked ahead");

		// Close the section traversal here: the head is about to become the other end of
		// the train, so it never reaches the head node of this section's last arc. The
		// turnback stand has already been booked into it by STDWL/ENDDWL.
		section_log_exit();

		// swap every occupied arc for its opposite and reverse the order, so the old
		// rear becomes the new front
		list<DirectedSegment*> flipped;
		for (auto arc : arcs)
			flipped.push_front(arc->segment->get_arc(arc->segment->other_node(arc->head)));

		// both positions are measured to the HEAD node of their own arc, so each
		// becomes the complement of the other against its segment's length
		double new_front_pos = arcs.back()->segment->length - back_pos;
		double new_back_pos = arcs.front()->segment->length - front_pos;

		arcs = flipped;
		front_pos = new_front_pos;
		back_pos = new_back_pos;
		forward = !forward;
		++reversal_count;

		// Drop the section granted in the old direction. This is what makes the rest
		// of Train::run work unchanged: with `section` null, the next request_access
		// is made from arcs.front() -- the NEW leading arc -- instead of from the far
		// end of the section just travelled.
		section = nullptr;
		new_section = nullptr;
		speed_profile.clear();
		sp_arc_ptr = nullptr;
		//the train now faces the other way, so any restriction it was still clearing is behind it
		transient_pending.clear();

		//The leading arc changed, so the persistent limit is picked up from it exactly as it is
		//when the front crosses a node. Where the new leading arc declares nothing the train keeps
		//what it carried in, which is the same rule an undeclared arc gets anywhere else; a
		//turnback platform that wants its own limit has to say so, in both directions.
		if (arcs.front()->is_speed_change && !arcs.front()->is_transient)
			current_speed_limit = arcs.front()->speed_limit;

		// Nothing was freed, but this train's `target` is about to change, and
		// SignalConstraint::is_violated does target matching -- so a request queued
		// against the old target may now be grantable. Nothing else would wake the
		// queue, because no segment changed state.
		signals_mgr->notify_segment_free();

		cout << "train " << id << " reversed at " << arcs.front()->str_id
			<< " (t=" << sim.time_now << ")\n";
		log_entry();
	}

	string Train::occupancy_str(bool forward_locked)
	{
		string str;
		if (forward_locked) {
			for (auto arc : locked)
				str += arc->str_id;
			return str;
		}
		for (auto arc : arcs)
			str += arc->str_id;
		return str;
	}

	///////////////////////////////////////Task

	Task::Task(Terminal* _terminal, bool _stop, double _dwell_time, double _earliest_departure) :
		terminal(_terminal), stop(_stop), dwell_time(_dwell_time),
		earliest_departure(_earliest_departure), booked(_terminal), id(task_counter++)
	{
	}

	Task::Task(const Task& rhs) :
		terminal(rhs.terminal), stop(rhs.stop), dwell_time(rhs.dwell_time),
		earliest_departure(rhs.earliest_departure), booked(rhs.booked), id(task_counter++)
	{
	}

	Task::Task() : terminal(nullptr), stop(false), dwell_time(0), earliest_departure(-1.0), booked(nullptr) {}

	////////////////////////////////////////Job

	Job::Job() : id(job_counter++) {}

	Job::~Job() {
		for (auto task : tasks)
			delete task;
		tasks.clear();
	}

	// defaults live on the declaration in desrail.h
	void Job::add_task(Terminal* terminal, bool stop, double dwell_time,
		double earliest_departure)
	{
		tasks.push_back(new Task(terminal, stop, dwell_time, earliest_departure));
	}

	////////////////////////////////////////TrainManagerBase

	TrainManagerBase::TrainManagerBase(Sim& _sim, RailNetwork* _network) :
		SimObject("TrainManager",_sim), network(_network)
	{
	}

	void TrainManagerBase::ready_signal(Train* train)
	{
		ready_trains.push_back(train);
		wakeup();		//no-op if the manager is already running (mid-drain); else resumes it to match
	}

	void TrainManagerBase::submit_job(Job* job)
	{
		pending_jobs.push_back(job);
		wakeup();		//no-op if the manager is already running; else resumes it to match
	}

	Job* TrainManagerBase::take_matching_job(const string& group)
	{
		for (auto it = pending_jobs.begin(); it != pending_jobs.end(); ++it) {
			if ((*it)->train_group == group) {		//exact-match, FIFO (oldest matching job)
				Job* job = *it;
				pending_jobs.erase(it);
				return job;
			}
		}
		return nullptr;
	}

	SimCoroutine TrainManagerBase::run()
	{
		while (true) {
			// Drain every currently-possible match. train->assign() may synchronously
			// resume the train, which can re-queue itself (a persistent train looping
			// back for its next job); we re-scan after each assignment to catch that,
			// so this loop settles only when no ready train has a compatible job.
			bool matched = true;
			while (matched) {
				matched = false;
				for (auto it = ready_trains.begin(); it != ready_trains.end(); ++it) {
					Job* job = take_matching_job((*it)->train_group);
					if (job != nullptr) {
						Train* train = *it;
						ready_trains.erase(it);
						train->assign(job);
						matched = true;
						break;		//iterators invalidated by erase + possible re-queue; restart the scan
					}
				}//train
			}//drain

			co_await sleep();	//woken by ready_signal / submit_job when either queue grows
		}//wend

		co_return;
	}

	////////////////////////////////////////SignalsManager
	SignalsManager::SignalsManager(Sim& _sim, RailNetwork* _network) :
		SimObject("SignalsManager", _sim), network(_network)
	{
		network->signals_mgr = this;
	}

	TrackSection* SignalsManager::request_access(Train* train, Terminal* target, DirectedSegment* from_arc) {
		TrackSection* section = grant_access(train, target, from_arc);
		if (section == nullptr)
			access_requests.push_back(new AccessRequest(train, target, from_arc, sim.time_now));
		return section;
	}

	//only to be called by Train::run()
	void SignalsManager::notify_segment_free()
	{
		if (access_requests.size() > 0)
			wakeup();
	}

	void SignalsManager::notify_active_train(Train* train)
	{
		active_trains.insert(train);
	}

	void SignalsManager::notify_inactive_train(Train* train)
	{
		active_trains.erase(train);
	}

	SimCoroutine SignalsManager::run()
	{
		//wakeup when segments freed and evaluate request queue
		while (true) {
			co_await sleep();
			auto it = access_requests.begin();
			while (it != access_requests.end()) {	//TODO: add train prioritisation
				AccessRequest* req = *it;
				TrackSection* section = grant_access(req->train, req->target, req->from_arc);
				if (section != nullptr) {
					it = access_requests.erase(it);
					req->train->notify_section_granted(section);
					delete req;
				}
				else
					++it;
			}//wend
		}//wend

		co_return;
	}

	// The classic policy: a section serves a request exactly when it reaches the
	// booked target, and granting it means nothing beyond the lock.
	bool SignalsManager::section_serves(TrackSection* section, Terminal* target,
		Train* train, DirectedSegment* from_arc, Terminal*& matched) const
	{
		matched = target;
		return section->reachable[target->id];
	}

	void SignalsManager::on_granted(Train* train, TrackSection* section, Terminal* target, Terminal* matched)
	{
	}

	TrackSection* SignalsManager::grant_access(Train* train, Terminal* target, DirectedSegment* from_arc)
	{
		for (auto section : from_arc->out_sections_ranked) {
			Terminal* matched = nullptr;
			if (section->is_available() && section_serves(section, target, train, from_arc, matched)) {
				//check for user specified occupancy constraints before granting section
				bool constraint_violation = false;
				auto to_arc = section->arcs.back();

				for (auto sc : to_arc->constraints) {
					bool result;
					if (!sc->seg_tuples.empty()) {
						// Generated constraint: segment-based all-occupied check
						result = sc->is_violated_seg(to_arc, train, section);
					} else {
						// Manual constraint: arc-based evaluation
						result = sc->is_violated(to_arc, train, section);
					}
					if (result) {
						constraint_violation = true;
						if (SignalConstraint::log_fires && SignalConstraint::fire_log && !sc->seg_tuples.empty()) {
							fprintf(SignalConstraint::fire_log, "CFIRE t=%.1f %s T%d(len=%.2f,%s->%s)",
								sim.time_now, sc->str_id.c_str(), train->id, train->length,
								from_arc->str_id.c_str(), target->name.c_str());
							for (auto& st : sc->seg_tuples) {
								if (st.front_signal == to_arc)
									fprintf(SignalConstraint::fire_log, " [s%d:REQ ml=%.2f xl=%.2f]", st.segment->id, st.min_length, st.max_length);
								else {
									Train* occ = st.segment->owner;
									if (occ)
										fprintf(SignalConstraint::fire_log, " [s%d:T%d(len=%.2f)@%s->%s fs=%d ml=%.2f xl=%.2f]", st.segment->id,
											occ->id, occ->length, occ->arcs.front()->str_id.c_str(),
											occ->target ? occ->target->name.c_str() : "?",
											st.front_signal->owner == occ ? 1 : 0,
											st.min_length, st.max_length);
								else
									fprintf(SignalConstraint::fire_log, " [s%d:FREE ml=%.2f xl=%.2f]", st.segment->id, st.min_length, st.max_length);
								}
							}
							fprintf(SignalConstraint::fire_log, "\n");
						}
						break;
					}
				}//sc

				if (!constraint_violation) {
					for (auto arc : section->arcs)
						arc->lock(train);
					// After the lock and before the return, so a policy that substitutes
					// a destination retargets the train on both grant paths. Constraint
					// evaluation above therefore sees the PRE-substitution target, which
					// is one reason a routing policy and the learned constraints are not
					// yet compatible.
					on_granted(train, section, target, matched);
					return section;
				}//if
			}//if
		}

		return nullptr;
	}

	SignalsManager::AccessRequest::AccessRequest(Train* _train, Terminal* _target, DirectedSegment* _from_arc, double _request_time) :
		train(_train), target(_target), from_arc(_from_arc), request_time(_request_time)
	{
	}

	const list<SignalsManager::AccessRequest*>& SignalsManager::get_access_requests() const
	{
		return access_requests;
	}

	bool SignalsManager::is_deadlocked() const
	{
		return active_trains.size() > 0 && access_requests.size() > 0
			&& sim.time_now < sim.max_time;
	}

	void SignalsManager::reset()
	{
		for (auto train : active_trains)
			delete train;
		active_trains.clear();
		for (auto req : access_requests)
			delete req;
		access_requests.clear();
	}

	double SpeedProfileArc::travel_time(double st_pos, double end_pos)
	{
		double t_st = 0;
		double t_end = 0;
		bool started = false;
		if (gt(st_pos, pos_coast, Train::eps)) {
			//start within acceleration phase
			started = true;
			t_st = accel_time(v0, accel, pos_start - st_pos);
			if (ge(end_pos, pos_coast, Train::eps))
				return accel_time(v0, accel, pos_start - end_pos) - t_st;
		}
		if (gt(st_pos, pos_decel, Train::eps)) {
			if (!started) {
				started = true;
				t_st = t_accel + (pos_coast - st_pos) / v_max;
			}
			if (ge(end_pos, pos_decel, Train::eps))
				return t_accel + (pos_coast - end_pos) / v_max - t_st;
		}
		if (!started)
			return accel_time(v_max, decel, pos_decel - end_pos) - accel_time(v_max, decel, pos_decel - st_pos);

		return t_accel + t_coast + accel_time(v_max, decel, pos_decel - end_pos) - t_st;
	}

	void SpeedProfileArc::set_phases(double _v0, double _t0, double accel_dist, double coast_dist, double decel_dist)
	{
		assert(eq(accel_dist + coast_dist + decel_dist, length, Train::eps));

		v0 = _v0;
		t0 = _t0;
		has_accel = true;
		has_coast = true;
		has_decel = true;

		if (eq(accel_dist, 0, Train::eps)) {
			has_accel = false;
			pos_coast = pos_start;
			t_accel = 0;
			v_max = v0;
		}
		else {
			pos_coast = pos_start - accel_dist;
			t_accel = accel_time(v0, accel, accel_dist);
			v_max = v0 + t_accel * accel;
		}

		if (eq(coast_dist, 0, Train::eps)) {
			has_coast = false;
			pos_decel = decel_dist + pos_end;
			t_coast = 0;
		}
		else {
			pos_decel = decel_dist + pos_end;
			t_coast = coast_dist / v_max;
		}

		if (eq(decel_dist, 0, Train::eps)) {
			has_decel = false;
			t_decel = 0;
		}
		else
			t_decel = accel_time(v_max, decel, decel_dist);
		
		t = t_accel + t_coast + t_decel;
		t1 = t0 + t;
		if (has_decel)
			v1 = v_max + decel * t_decel;
		else
			v1 = v_max;
	}//set_phases

	void SpeedProfileArc::set_parameters(Train* train, DirectedSegment* _arc, int _seq, double _d, double _pos_start, double _pos_end)
	{
		accel = train->accel;
		decel = -train->decel;
		arc = _arc;
		seq = _seq;
		d = _d;
		length = _pos_start - _pos_end;
		pos_start = _pos_start;
		pos_end = _pos_end;
		continues_in_next = false;	//overwritten by the caller for a same-arc split's first span
	}

	inline double SpeedProfileArc::accel_time(double v_st, double a, double dist)
	{
		return (-v_st + ssqrt(v_st * v_st + 2 * a * dist, Train::eps)) / a;
	}

	double SpeedProfileArc::get_next_accel_change_pos(double front_pos)
	{
		double change_pos;
		if (gt(front_pos, pos_coast, Train::eps)) {
			//accelerating
			change_pos = pos_coast;
		}
		else if (gt(front_pos, pos_decel, Train::eps)) {
			//coasting
			change_pos = pos_decel;
		}
		else
			return 0;		
		if (eq(change_pos, 0, Train::eps))
			return 0;
		return change_pos;
	}

	double SpeedProfileArc::get_inst_speed(double front_pos)
	{
		if (has_accel && ge(front_pos, pos_coast, Train::eps)) {
			//in acceleration phase
			double a_time = accel_time(v0, accel, pos_start - front_pos);
			return v0 + accel * a_time;
		}
		if (has_coast && ge(front_pos, pos_decel, Train::eps))
			//in coasting phase
			return v_max;
		if (!has_decel)
			//Degenerate span (zero length): nothing to evaluate, so report the entry speed
			//rather than aborting. Construction already warned; this only stops one bad span
			//from taking the whole run down.
			return v0;
		double a_time = accel_time(v_max, decel, pos_decel - front_pos);
		return v_max + decel * a_time;
	}

	////////////////////////////////////////SignalConstraint

	void SignalConstraint::add_constraint_arc(DirectedSegment* arc, Terminal* target, DirectedSegment* front_signal)
	{
		c_arcs.push_back(new ConstraintArc(arc, target, front_signal));
		if (front_signal) {
			// Generated constraint: register on front_signal for discovery
			assert(front_signal->is_signal);
			front_signal->constraints.push_back(this);
		} else {
			// Manual constraint: existing behavior
			assert(arc->is_signal);
			arc->constraints.push_back(this);
		}
	}

	bool SignalConstraint::is_violated(DirectedSegment* arc_req, Train* train, TrackSection* section) {
		set<Train*> occupants;

		for (auto c_arc : c_arcs) {
			if (c_arc->front_signal) {
				// === Generated constraint: segment->owner + front signal position match ===
				if (c_arc->front_signal == arc_req) {
					bool route_match = false;

					// Fast path: blocking arc's segment is in the requested section
					if (section) {
						for (auto sa : section->arcs)
							if (sa->segment == c_arc->arc->segment) { route_match = true; break; }
					}

					// Fallback: train currently occupies the blocking segment —
					// check if it will still occupy it after traversing the section.
					// Walk from section boundary backward: locked (reverse) then arcs (forward).
					if (!route_match && section && c_arc->arc->segment->owner == train) {
						double section_dist = section->length - section->arcs.back()->margin;
						double remaining = train->length - section_dist;
						if (remaining > 0) {
							// locked arcs: back = section boundary signal, front = next to physically enter
							for (auto it = train->locked.rbegin(); !route_match && it != train->locked.rend(); ++it) {
								if ((*it)->segment == c_arc->arc->segment) { route_match = true; break; }
								remaining -= (*it)->segment->length;
								if (remaining <= 0) break;
							}
							// physical arcs: front = physical train front, back = tail
							for (auto it = train->arcs.begin(); !route_match && remaining > 0 && it != train->arcs.end(); ++it) {
								if ((*it)->segment == c_arc->arc->segment) { route_match = true; break; }
								remaining -= (*it)->segment->length;
								if (remaining <= 0) break;
							}
						}
					}

					if (route_match && is_target_match(c_arc, train))
						occupants.insert(train);
				}
				else if (c_arc->arc->segment->owner && is_target_match(c_arc, c_arc->arc->segment->owner)) {
					// Existing occupant: segment->owner + front signal position match
					Train* tr = c_arc->arc->segment->owner;
					if (c_arc->front_signal->owner == tr)
						occupants.insert(tr);
				}
			} else {
				// === Manual constraint: existing logic unchanged ===
				if (c_arc->arc == arc_req) {
					if (is_target_match(c_arc, train))
						occupants.insert(train);
				} else {
					if (c_arc->arc->owner && is_target_match(c_arc, c_arc->arc->owner)) {
						Train* tr = c_arc->arc->owner;
						if (tr->arcs.front() == c_arc->arc || (!tr->locked.empty() && tr->locked.back() == c_arc->arc))
							occupants.insert(c_arc->arc->owner);
					}
				}
			}
		}//c_arc

		return occupants.size() > occupancy_limit;
	}

	bool SignalConstraint::tail_clears_segment(Train* train, TrackSegment* target_seg, double tail_behind) {
		// Caller must guard against tail_behind <= 0 before calling.
		// (Semantics of tail_behind <= 0 differ by call site.)

		// Walk backwards from section start: locked arcs (back to front),
		// then occupied arcs (front to back)
		double dist = 0;
		bool found = false;
		for (auto it = train->locked.rbegin(); !found && it != train->locked.rend(); ++it) {
			if ((*it)->segment == target_seg) {
				found = true;
				if (gt(dist, tail_behind, Train::eps))
					return true;  // tail doesn't reach target_seg, cleared
			}
			else {
				dist += (*it)->segment->length;
				if (gt(dist, tail_behind, Train::eps))
					return true;  // tail can't reach this far
			}
		}
		for (auto it = train->arcs.begin(); !found && it != train->arcs.end(); ++it) {
			if ((*it)->segment == target_seg) {
				found = true;
				if (gt(dist, tail_behind, Train::eps))
					return true;  // tail doesn't reach target_seg, cleared
			}
			else {
				dist += (*it)->segment->length;
				if (gt(dist, tail_behind, Train::eps))
					return true;  // tail can't reach this far
			}
		}
		return false;  // train would NOT clear target_seg
	}

	bool SignalConstraint::is_violated_seg(DirectedSegment* arc_req, Train* train, TrackSection* section) {
		for (auto& st : seg_tuples) {
			Train* owner = nullptr;
			if (st.front_signal == arc_req) {  //check if seg-tuple corresponds to requesting train
				owner = train;
				if (st.segment != arc_req->segment) {
					bool found = false;  //need to check if segment is within requested section
					for (auto arc : section->arcs) {
						if (arc->segment == st.segment) {
							found = true;
							break;
						}
					}//arc
					if (!found) {
						// Segment not in requested section — check if train's tail still occupies it
						if (st.segment->owner != train)
							return false;  //train doesn't occupy this segment either, constraint does not apply
						// Tail clearance: would the train's tail clear this segment
						// after advancing through the requested section?
						double tail_behind = train->length - section->length + arc_req->margin;
						if (lt(tail_behind, 0, Train::eps))
							return false;  // tail within requested section, clears st.segment
						if (tail_clears_segment(train, st.segment, tail_behind))
							return false;
					}
				}//if
			}
			else {
				owner = st.segment->owner;
				if (!owner)
					return false;  //segment has no owner - constraint does not apply
				DirectedSegment* owner_front = (owner == train) ? arc_req : (owner->locked.empty() ? owner->arcs.front() : owner->locked.back());
				if (owner_front != st.front_signal)
					return false;  //owner's front position doesn't match expected front_signal - constraint does not apply

				// If the occupant IS the requesting train, check if it would clear
				// this segment after advancing through the requested section.
				if (owner == train) {
					double tail_behind = train->length - section->length + arc_req->margin;
					if (lt(tail_behind,0,Train::eps))
						return false;  // tail within requested section, clears st.segment
					if (tail_clears_segment(train, st.segment, tail_behind))
						return false;
				}
				// Check if the occupant's tail would clear this segment
				// after finishing its current section traversal.
				else {
					if (gt(owner->v_inst, 0, Train::eps)) {
						//double tail_behind = owner->length - owner->section->arcs.back()->margin;
						double tail_behind = owner->section->arcs.back()->margin + owner->length;
						if (tail_clears_segment(owner, st.segment, tail_behind))
							return false;
					}
				}
			}
			if (!owner) 
				return false;		//segment has no owner - constraint does not apply
			if (!is_target_match_seg(st.target, owner))
				return false;		//segment owner is heading for an incompatible target - constraint does not apply
			if (gt(st.min_length, 0.0, Train::eps) && lt(owner->length, st.min_length, Train::eps))
				return false;		//train too short for this tuple
			if (gt(st.max_length, 0.0, Train::eps) && gt(owner->length, st.max_length, Train::eps))
				return false;		//train too long for this tuple
		}
		// DEBUG: break when a constraint learned after seed 48's training (id > 397) fires
		return true;	//all conditions of constraint violation have been met
	}
	/*
	bool SignalConstraint::is_violated_seg(DirectedSegment* arc_req, Train* train, TrackSection* section) {
		if (seg_tuples.empty())
			return false;	// not a generated constraint or seg_tuples not populated

		// Per-front-signal group check: violated iff every distinct front_signal
		// position has a compatible occupant. A front_signal is "satisfied" when
		// any of its seg_tuples has a valid occupant. This handles multi-segment
		// spanning (more seg_tuples than front_signals) correctly.
		set<DirectedSegment*> satisfied;

		for (auto& st : seg_tuples) {
			if (satisfied.count(st.front_signal))
				continue;	// this front_signal group already satisfied

			if (st.front_signal == arc_req) {
				// === Requesting train check ===
				if (is_target_match_seg(st.target, train)) {
					bool route_match = false;

					// Fast path: segment is in the requested section
					if (section) {
						for (auto sa : section->arcs)
							if (sa->segment == st.segment) { route_match = true; break; }
					}

					// Fallback: multi-section footprint check
					if (!route_match && section && st.segment->owner == train) {
						double section_dist = section->length - section->arcs.back()->margin;
						double remaining = train->length - section_dist;
						if (remaining > 0) {
							for (auto it = train->locked.rbegin(); !route_match && it != train->locked.rend(); ++it) {
								if ((*it)->segment == st.segment) { route_match = true; break; }
								remaining -= (*it)->segment->length;
								if (remaining <= 0) break;
							}
							for (auto it = train->arcs.begin(); !route_match && remaining > 0 && it != train->arcs.end(); ++it) {
								if ((*it)->segment == st.segment) { route_match = true; break; }
								remaining -= (*it)->segment->length;
								if (remaining <= 0) break;
							}
						}
					}

					if (route_match)
						satisfied.insert(st.front_signal);
				}
			}
			else if (st.segment->owner && st.front_signal->owner == st.segment->owner) {
				// === Existing occupant check ===
				Train* tr = st.segment->owner;
				if (is_target_match_seg(st.target, tr))
					satisfied.insert(st.front_signal);
			}
		}

		return (int)satisfied.size() > occupancy_limit;
	}*/

	bool SignalConstraint::verify_seg_mode = false;
	bool SignalConstraint::log_fires = false;
	FILE* SignalConstraint::fire_log = nullptr;

	bool SignalConstraint::is_target_match(ConstraintArc* c_arc, Train* train)
	{
		if (!c_arc->target)
			return true;
		if (c_arc->target == train->target)
			return true;
		if (!train->job)
			return false;
		for (auto task : train->job->tasks) {
			if (c_arc->target == task->terminal)
				return true;
		}
		return false;
	}

	bool SignalConstraint::is_target_match_seg(Terminal* constraint_target, Train* train)
	{
		if (!constraint_target)
			return true;
		if (constraint_target == train->target)
			return true;
		if (!train->job)
			return false;
		for (auto task : train->job->tasks) {
			if (constraint_target == task->terminal)
				return true;
		}
		return false;
	}

}//DesRail namespace