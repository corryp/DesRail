#include "deadlock_avoidance.h"
#include "rail_sim_tools.h"
#include "read_csv.h"
#include <iostream>
#include <algorithm>
#include <chrono>
#include <tuple>
#include <limits>

namespace DesRailDL {

	int generated_constraint_counter = 0;

	// A5 footprint format. The second column holds the occupancy limit on a
	// constraint's first row and the footprint on its tuple rows; a footprint is
	// "seg:head" arcs joined by '>', head first.
	const char* CONSTRAINT_FILE_HEADER =
		"constraint_id,footprint/occupancy_limit,terminal,min_length";

	////////////////////////////////////////ConflictGraph

	ConflictGraph::ConflictGraph() : omega(nullptr) {}

	ConflictGraph::~ConflictGraph()
	{
		for (auto node : nodes)
			delete node;
	}

	ConflictNode* ConflictGraph::get_or_create_node(Train* train, DirectedSegment* from_arc, Terminal* target)
	{
		auto it = train_to_node.find(train);
		if (it != train_to_node.end())
			return it->second;

		ConflictNode* node = new ConflictNode((int)nodes.size(), train, from_arc, target);
		nodes.push_back(node);
		adjacency.push_back(vector<int>());
		train_to_node[train] = node;
		return node;
	}

	void ConflictGraph::add_edge(int from, int to, string label)
	{
		// Deduplicate adjacency (Tarjan's doesn't need parallel entries)
		// but always record labels — a pair can have multiple blocking reasons
		bool exists = false;
		for (int neighbor : adjacency[from])
			if (neighbor == to) { exists = true; break; }
		if (!exists)
			adjacency[from].push_back(to);
		if (!label.empty())
			edge_labels[{from, to}].push_back(label);
	}

	void ConflictGraph::build_from_state(SignalsManager* signals_mgr, RailNetwork* network)
	{
		// Create omega node (sink for trains that can progress)
		omega = new ConflictNode((int)nodes.size(), nullptr, nullptr, nullptr);
		omega->is_omega = true;
		nodes.push_back(omega);
		adjacency.push_back(vector<int>());

		// Collect trains that have pending access requests
		set<Train*> blocked_trains;
		for (auto req : signals_mgr->get_access_requests())
			blocked_trains.insert(req->train);

		// Create nodes for blocked trains and find blockers. For each candidate
		// section we record ALL blockers grouped into reasons (a physical occupant
		// is a singleton reason; a violated constraint is the reason of its
		// tuple-trains). Edges/edge_blocking_arcs are still populated for DOT
		// export and generation; node_sections is the authoritative reason view.
		for (auto req : signals_mgr->get_access_requests()) {
			ConflictNode* req_node = get_or_create_node(req->train, req->from_arc, req->target);

			// Mirror grant_access logic: iterate over candidate sections
			for (auto section : req->from_arc->out_sections_ranked) {
				if (!section->reachable[req->target->id])
					continue;

				CandidateSection cs;
				cs.sec_id = section->arcs.back()->str_id;

				if (!section->is_available()) {
					// Physical blocking: record EVERY occupant (each a singleton
					// reason — the section is blocked while any one is present).
					string sec_label = section->arcs.front()->str_id + ".." + section->arcs.back()->str_id;
					for (auto arc : section->arcs) {
						if (arc->segment->owner != nullptr && arc->segment->owner != req->train) {
							Train* blocker = arc->segment->owner;
							ConflictNode* blocker_node = get_or_create_node(blocker, nullptr, blocker->target);
							add_edge(req_node->id, blocker_node->id, "S:" + sec_label + " @" + arc->str_id);
							edge_blocking_arcs[{req_node->id, blocker_node->id}].push_back(arc);
							BlockReason r;
							r.trains.push_back({blocker_node->id, arc});
							cs.reasons.push_back(r);
						}
					}
				}
				else {
					// Section physically available: each violated constraint is one
					// reason (all its tuple-trains must be present for it to fire).
					auto to_arc = section->arcs.back();
					for (auto sc : to_arc->constraints) {
						bool violated = !sc->seg_tuples.empty()
							? sc->is_violated_seg(to_arc, req->train, section)
							: sc->is_violated(to_arc, req->train, section);
						if (!violated)
							continue;

						BlockReason r;
						r.cid = sc->str_id;
						if (!sc->seg_tuples.empty()) {
							// Generated constraint: blockers from seg_tuples
							// A5: one tuple per train, so each tuple contributes AT MOST ONE
							// blocker to the reason. The deepest arc of the footprint is the
							// one that identifies the occupant, and because occupancy is
							// contiguous from the head it also implies the rest. This is
							// where the old form pushed a blocker once per occupied segment
							// and so named the same train twice (comment 2.12).
							for (auto& st : sc->seg_tuples) {
								if (st.arcs.empty())
									continue;
								if (st.front_signal() == to_arc) {
									// This tuple is matched against the requesting train
									// itself. It names no blocker, but the arcs it pins are
									// load-bearing for the reason, so record them.
									for (auto a : st.arcs)
										r.self_arcs.push_back(a);
									continue;
								}
								TrackSegment* deep = st.deepest()->segment;
								if (deep->owner == nullptr)
									continue;
								Train* blocker = deep->owner;
								if (blocker == req->train || st.front_signal()->owner != blocker)
									continue;
								ConflictNode* blocker_node = get_or_create_node(blocker, nullptr, blocker->target);
								add_edge(req_node->id, blocker_node->id, "C:" + sc->str_id + " @" + to_arc->str_id);
								// Record an arc of the occupied segment, not the front signal.
								DirectedSegment* seg_arc = deep->forward_arc ? deep->forward_arc : deep->reverse_arc;
								edge_blocking_arcs[{req_node->id, blocker_node->id}].push_back(seg_arc);
								r.trains.push_back({blocker_node->id, seg_arc});
							}
						} else {
							// Manual constraint: existing c_arcs path
							for (auto c_arc : sc->c_arcs) {
								if (c_arc->arc != to_arc && c_arc->arc->segment->owner != nullptr) {
									Train* blocker = c_arc->arc->segment->owner;
									if (blocker != req->train) {
										ConflictNode* blocker_node = get_or_create_node(blocker, nullptr, blocker->target);
										add_edge(req_node->id, blocker_node->id, "C:" + sc->str_id + " @" + to_arc->str_id);
										edge_blocking_arcs[{req_node->id, blocker_node->id}].push_back(c_arc->arc);
										r.trains.push_back({blocker_node->id, c_arc->arc});
									}
								}
							}
						}
						if (!r.trains.empty())
							cs.reasons.push_back(r);
					}
				}

				// Only blocked sections (>=1 reason) matter; an empty-reason section
				// would be passable and the train would not be blocked on it.
				if (!cs.reasons.empty())
					node_sections[req_node->id].push_back(cs);
			}
		}

		// Active trains NOT in access_requests get an edge to omega
		for (auto train : signals_mgr->active_trains) {
			if (blocked_trains.find(train) == blocked_trains.end()) {
				ConflictNode* node = get_or_create_node(train, nullptr, train->target);
				add_edge(node->id, omega->id);
			}
		}
	}

	// AND-OR (reason-based) reachability to omega. A node is free iff it can
	// eventually move: it has a passable section, where a section is passable iff
	// every reason is defeated (each reason has >=1 train that is itself free).
	// Base case: omega and any node with no blocked candidate sections (a
	// progressing train, or one not requesting access) is free. Monotone
	// least-fixpoint, so the result is the maximal set of trains that can escape;
	// whatever is left is genuinely stuck. Edges to free nodes are "ghosts".
	set<int> ConflictGraph::compute_free_nodes() const
	{
		set<int> free;
		for (auto node : nodes) {
			if (node->is_omega || node_sections.find(node->id) == node_sections.end())
				free.insert(node->id);
		}
		bool changed = true;
		while (changed) {
			changed = false;
			for (auto& [u, sections] : node_sections) {
				if (free.count(u))
					continue;
				bool any_passable = false;
				for (auto& cs : sections) {
					bool passable = true;
					for (auto& r : cs.reasons) {
						bool defeated = false;
						for (auto& [v, arc] : r.trains)
							if (free.count(v)) { defeated = true; break; }
						if (!defeated) { passable = false; break; }
					}
					if (passable) { any_passable = true; break; }
				}
				if (any_passable) { free.insert(u); changed = true; }
			}
		}
		return free;
	}

	////////////////////////////////////////Tarjan's SCC

	struct TarjanState {
		vector<int> index_of;
		vector<int> lowlink;
		vector<bool> on_stack;
		stack<int> st;
		int current_index;
		SCCResult result;

		TarjanState(int n) : index_of(n, -1), lowlink(n, 0), on_stack(n, false), current_index(0) {}
	};

	static void tarjan_strongconnect(int v, const vector<vector<int>>& adj, TarjanState& state)
	{
		state.index_of[v] = state.current_index;
		state.lowlink[v] = state.current_index;
		state.current_index++;
		state.st.push(v);
		state.on_stack[v] = true;

		for (int w : adj[v]) {
			if (state.index_of[w] == -1) {
				tarjan_strongconnect(w, adj, state);
				state.lowlink[v] = min(state.lowlink[v], state.lowlink[w]);
			}
			else if (state.on_stack[w]) {
				state.lowlink[v] = min(state.lowlink[v], state.index_of[w]);
			}
		}

		if (state.lowlink[v] == state.index_of[v]) {
			vector<int> component;
			int w;
			do {
				w = state.st.top();
				state.st.pop();
				state.on_stack[w] = false;
				component.push_back(w);
			} while (w != v);
			state.result.components.push_back(component);
		}
	}

	SCCResult tarjan_scc(const vector<vector<int>>& adjacency, int n)
	{
		TarjanState state(n);

		for (int v = 0; v < n; v++) {
			if (state.index_of[v] == -1)
				tarjan_strongconnect(v, adjacency, state);
		}

		return state.result;
	}

	////////////////////////////////////////DeadlockAnalyzer

	// On by default — absent PRUNING option in dlexp_options.csv leaves pruning enabled.
	bool DeadlockAnalyzer::pruning_enabled = true;
	// Generous by default; the worst core measured proved minimal in 1,339
	// expansions (revision/D3_pruning_measurement/alg2_design).
	long DeadlockAnalyzer::prune_search_cap = 20000;
	long DeadlockAnalyzer::search_proven = 0;
	long DeadlockAnalyzer::search_capped = 0;
	long DeadlockAnalyzer::trains_pruned = 0;

	DeadlockAnalyzer::DeadlockAnalyzer(SignalsManager* _signals_mgr, RailNetwork* _network)
		: signals_mgr(_signals_mgr), network(_network), write_dot(false), scc_log(nullptr), iteration(-1) {}

	bool DeadlockAnalyzer::analyze()
	{
		generated_constraints.clear();

		ConflictGraph graph;
		graph.build_from_state(signals_mgr, network);

		if (graph.nodes.size() <= 1)
			return false;	// Only omega or empty

		// AND-OR reachability: classify every node as free (can reach omega) or
		// stuck. Edges to free nodes are "ghosts" — they established a path to omega
		// (already consumed by the fixpoint) and play no part in the deadlock.
		set<int> free = graph.compute_free_nodes();

		// Stuck/real subgraph: keep only edges between stuck nodes (drop ghosts).
		int n = (int)graph.nodes.size();
		vector<vector<int>> real_adj(n);
		for (int u = 0; u < n; u++) {
			if (free.count(u))
				continue;	// free nodes carry no deadlock structure
			for (int v : graph.adjacency[u])
				if (!free.count(v))
					real_adj[u].push_back(v);
		}

		SCCResult scc = tarjan_scc(real_adj, n);

		// 1. Deadlock SCCs = non-singleton SCCs of the stuck subgraph. Stuck nodes
		//    cannot reach omega by construction, so the old omega/escape checks are
		//    no longer needed — they are subsumed by the free/stuck fixpoint.
		vector<vector<int>> deadlock_sccs;
		vector<int> node_to_dl_scc(n, -1);

		for (auto& component : scc.components) {
			if (component.size() <= 1)
				continue;
			int dl_idx = (int)deadlock_sccs.size();
			for (int idx : component)
				node_to_dl_scc[idx] = dl_idx;
			deadlock_sccs.push_back(component);
		}

		// 1b. Discard SCCs containing trains that are still moving.
		//     If a train has v_inst > 0, the conflict graph snapshot captured
		//     a transient state (e.g. delayed segment release pending).
		//     Wait for the next check interval when all trains have settled.
		{
			vector<vector<int>> settled_sccs;
			for (auto& comp : deadlock_sccs) {
				bool all_stopped = true;
				for (int idx : comp) {
					if (graph.nodes[idx]->train && gt(graph.nodes[idx]->train->v_inst, 0.0, Train::eps)) {
						all_stopped = false;
						break;
					}
				}
				if (all_stopped)
					settled_sccs.push_back(comp);
			}
			deadlock_sccs = settled_sccs;
			// Rebuild node_to_dl_scc mapping after filtering
			fill(node_to_dl_scc.begin(), node_to_dl_scc.end(), -1);
			for (int si = 0; si < (int)deadlock_sccs.size(); si++)
				for (int idx : deadlock_sccs[si])
					node_to_dl_scc[idx] = si;
		}

		if (deadlock_sccs.empty())
			return false;

		// 2. Classify each deadlock SCC as root-cause or derived by SELF-CONTAINMENT
		//    (Def: Root-Cause SCC). A deadlock SCC is root-cause iff it is
		//    self-contained: every candidate section of every member train has a
		//    blocking reason lying entirely within the SCC. A non-self-contained
		//    SCC is derived — some member's stuck-ness depends on a train outside
		//    the SCC (e.g. an escape section blocked only by an external constraint)
		//    — and generates no constraint; resolving its root cause frees it in
		//    cascade (Lemma: Root causes resolve all deadlocks). This replaces the
		//    earlier condensed-DAG reachability test, which could mislabel such a
		//    derived SCC as root-cause and emit an over-restrictive cut.
		vector<bool> is_root_cause(deadlock_sccs.size(), false);
		for (int si = 0; si < (int)deadlock_sccs.size(); si++) {
			set<int> scc_set(deadlock_sccs[si].begin(), deadlock_sccs[si].end());
			is_root_cause[si] = is_self_contained(graph, scc_set);
		}

		// 3. Prune redundant constraint edges from each root-cause SCC.
		//    If a node has outgoing SCC-internal edges from multiple constraints
		//    (or physical + constraint), some constraint edges may be redundant.
		//    Pruning condition: every affected node must retain at least one
		//    SCC-internal outbound edge after removing the candidate constraint's edges.
		set<string> all_pruned_cids;
		set<int> all_pruned_nodes;
		// What the search did to each root-cause SCC, kept for the SCC log. Only
		// the aggregate counters recorded this before, and they go to stdout,
		// which the harness overwrites every iteration. Indexed by si, so sized
		// to every SCC and filled only for the root causes.
		vector<int> pre_prune_sizes(deadlock_sccs.size(), -1);
		vector<char> scc_capped(deadlock_sccs.size(), 0);
		for (int si = 0; pruning_enabled && si < (int)deadlock_sccs.size(); si++) {
			if (!is_root_cause[si])
				continue;
			int pre_prune_size = (int)deadlock_sccs[si].size();
			set<int> pre_prune_set(deadlock_sccs[si].begin(), deadlock_sccs[si].end());
			vector<string> pruned_cids;
			deadlock_sccs[si] = prune_to_minimal_core(graph, deadlock_sccs[si], pruned_cids);
			int post_prune_size = (int)deadlock_sccs[si].size();
			bool capped = find(pruned_cids.begin(), pruned_cids.end(),
				string("CAP")) != pruned_cids.end();
			pre_prune_sizes[si] = pre_prune_size;
			scc_capped[si] = capped ? 1 : 0;

			if (post_prune_size < pre_prune_size) {
				cout << "  SCC " << si << " pruned from " << pre_prune_size << " to " << post_prune_size << " nodes" << endl;
				// Join cids with '|' (not ',') so the pruning_details CSV column
				// stays single-field — a multi-cid full-backer-set prune writes
				// e.g. "4>3[-dl_14|dl_15]" without breaking comma-delimited CSV.
				string summary = to_string(pre_prune_size) + ">" + to_string(post_prune_size) + "[-";
				for (int i = 0; i < (int)pruned_cids.size(); i++) {
					if (i > 0) summary += "|";
					summary += pruned_cids[i];
				}
				summary += "]";
				prune_log.push_back(summary);
				// A capped core that shrank records CAP among the dropped ids.
				// Track pruned constraint IDs and dropped nodes for DOT visualization
				all_pruned_cids.insert(pruned_cids.begin(), pruned_cids.end());
				set<int> post_prune_set(deadlock_sccs[si].begin(), deadlock_sccs[si].end());
				for (int node_idx : pre_prune_set)
					if (post_prune_set.find(node_idx) == post_prune_set.end())
						all_pruned_nodes.insert(node_idx);
			}
			else if (capped) {
				// One that did not shrink left no trace before, so it read as a
				// core the search had proven minimal. The cut is still valid,
				// since the core is self-contained either way, but the
				// minimality the theorem asks for is unproven for it, and that
				// is the distinction the record has to carry.
				prune_log.push_back(to_string(pre_prune_size) + ">"
					+ to_string(post_prune_size) + "[CAP]");
			}
		}

		// 4. Generate constraints for root-cause SCCs
		bool found_deadlock = false;
		for (int si = 0; si < (int)deadlock_sccs.size(); si++) {
			if (is_root_cause[si]) {
				found_deadlock = true;
				GeneratedConstraint gc = generate_constraint_from_scc(graph, deadlock_sccs[si]);
				generated_constraints.push_back(gc);
			}
		}


		// 4b. Write SCC log (one row per SCC node, grouped by SCC)
		if (found_deadlock && scc_log) {
			int gc_idx = 0;
			for (int si = 0; si < (int)deadlock_sccs.size(); si++) {
				if (!is_root_cause[si])
					continue;
				auto& comp = deadlock_sccs[si];
				set<int> scc_set(comp.begin(), comp.end());

				// Classify edges: physical, constraint, or mixed
				bool has_physical = false, has_constraint = false;
				for (int u : comp) {
					for (int v : graph.adjacency[u]) {
						if (scc_set.find(v) == scc_set.end()) continue;
						auto eit = graph.edge_labels.find({u, v});
						if (eit != graph.edge_labels.end()) {
							for (auto& lbl : eit->second) {
								if (lbl.substr(0, 2) == "S:") has_physical = true;
								else if (lbl.substr(0, 2) == "C:") has_constraint = true;
							}
						}
					}
				}
				string edge_type = (has_physical && has_constraint) ? "mixed" :
					(has_physical ? "physical" : "constraint");

				// Find which constraint was generated for this SCC
				string gc_id = (gc_idx < (int)generated_constraints.size()) ? generated_constraints[gc_idx].str_id : "?";
				gc_idx++;

				for (int idx : comp) {
					ConflictNode* node = graph.nodes[idx];
					if (!node->train) continue;
					Train* t = node->train;
					string front_arc = t->arcs.empty() ? "?" :
						(t->locked.empty() ? t->arcs.front()->str_id : t->locked.back()->str_id);
					string target = (node->target != nullptr) ? node->target->name : "NA";
					char buf[256];
					snprintf(buf, sizeof(buf), "%d,%s,%.1f,%d,T%d,%.2f,%s,%s,%s,%d,%d\n",
						iteration, gc_id.c_str(), signals_mgr->sim.time_now,
						(int)comp.size(), t->id, t->length,
						front_arc.c_str(), target.c_str(),
						edge_type.c_str(),
						pre_prune_sizes[si] >= 0 ? pre_prune_sizes[si] : (int)comp.size(),
						(int)scc_capped[si]);
					*scc_log << buf;
				}
			}
			scc_log->flush();
		}

		if (found_deadlock && write_dot)
			write_conflict_graph_dot(graph, scc, dot_path, signals_mgr->sim.time_now, all_pruned_cids, all_pruned_nodes);

		return found_deadlock;
	}

	vector<GeneratedConstraint> DeadlockAnalyzer::get_generated_constraints() const
	{
		return generated_constraints;
	}

	// Extract constraint ID from edge label. Returns "" for physical edges.
	static string get_constraint_id(const string& label)
	{
		// Constraint labels: "C:dl_123 @(arc)"
		// Physical labels:   "S:(first)..(last) @(arc)"
		if (label.size() >= 2 && label[0] == 'C' && label[1] == ':') {
			size_t space = label.find(' ');
			return (space != string::npos) ? label.substr(2, space - 2) : label.substr(2);
		}
		return "";
	}

	// A set of trains S is self-contained (Def: Section coverage; self-contained
	// set) iff every candidate section of every train in S has at least one
	// blocking reason lying entirely within S — so the constraint that encodes
	// that reason keeps the section blocked whenever it fires, with no reliance on
	// a train outside S (which a future firing need not reproduce). Root-cause
	// deadlock SCCs are exactly the self-contained ones (Def: Root-Cause SCC); the
	// pruner also uses this to trim a root-cause SCC to its minimal self-contained
	// core.
	bool DeadlockAnalyzer::is_self_contained(ConflictGraph& graph, const set<int>& S) const
	{
		int node, sec;
		return !uncovered_section(graph, S, node, sec);
	}

	// Distinct blocker nodes named by a reason. BlockReason::trains is a vector
	// and can name one train twice (a blocker occupying two of a constraint's
	// tuple segments, the surplus-tuple mechanism), so set semantics matter: the
	// search ranks reasons by how many DISTINCT trains they name.
	static set<int> reason_train_set(const BlockReason& r)
	{
		set<int> out;
		for (auto& tr : r.trains)
			out.insert(tr.first);
		return out;
	}

	// The in-S reasons for one candidate section, each as its distinct train set,
	// paired with how many of those trains S does not already hold. Ordering on
	// that pair and then on the train set itself makes every choice below
	// deterministic, which matters because the campaign compares the Windows and
	// Linux builds core for core.
	static vector<pair<size_t, set<int>>> in_core_choices(
		const CandidateSection& cs, const set<int>& scc_set, const set<int>& U)
	{
		vector<pair<size_t, set<int>>> out;
		for (auto& r : cs.reasons) {
			set<int> ts = reason_train_set(r);
			bool in_scc = true;
			for (int t : ts)
				if (!scc_set.count(t)) { in_scc = false; break; }
			if (!in_scc)
				continue;
			size_t fresh = 0;
			for (int t : ts)
				if (!U.count(t))
					fresh++;
			out.push_back(make_pair(fresh, ts));
		}
		sort(out.begin(), out.end());
		return out;
	}

	// First candidate section of a member of S with no reason lying inside S.
	// Returning false means every section of every member is covered from within
	// S, which is exactly self-containment, so is_self_contained is this function
	// with its answer thrown away.
	bool DeadlockAnalyzer::uncovered_section(
		ConflictGraph& graph, const set<int>& S, int& node, int& sec_idx) const
	{
		for (int u : S) {
			auto it = graph.node_sections.find(u);
			if (it == graph.node_sections.end())
				continue;
			for (int k = 0; k < (int)it->second.size(); k++) {
				bool covered = false;
				for (auto& r : it->second[k].reasons) {
					bool all_in = true;
					for (auto& tr : r.trains)
						if (!S.count(tr.first)) { all_in = false; break; }
					if (all_in) { covered = true; break; }
				}
				if (!covered) {
					node = u;
					sec_idx = k;
					return true;
				}
			}
		}
		return false;
	}

	// Phase 1. Grow a self-contained set outward from one seed train: take a
	// section of the set in hand that no in-core reason covers, add the reason
	// that brings in fewest new trains, repeat until every section of every
	// member is covered. The result is self-contained by construction, so it is
	// always a usable core; it need not be minimal, which is what phase 2 is for.
	//
	// Terminates within |scc_set| steps, since the chosen reason always names a
	// train not yet held (or the section would already be covered). Cannot dead
	// end either: the root-cause SCC is self-contained (Def: Root-Cause SCC), so
	// every section of every member has at least one reason lying inside it.
	//
	// Measured over 3,265 cores (revision/D3_pruning_measurement/alg2_design):
	// the best dive over all seeds attained the proven minimum on every one, in a
	// median of 12 steps and at worst 268. The fewest-new-trains rule is doing
	// that work, not the dive itself: lowest-id-first reaches the minimum on
	// 3,225 and most-new-trains-first on 3,214.
	set<int> DeadlockAnalyzer::dive_core(
		ConflictGraph& graph, const set<int>& scc_set, int seed) const
	{
		set<int> U;
		U.insert(seed);

		int node, sec;
		while (uncovered_section(graph, U, node, sec)) {
			vector<pair<size_t, set<int>>> choices =
				in_core_choices(graph.node_sections.at(node)[sec], scc_set, U);
			if (choices.empty())
				return U;	// defensive: unreachable from a self-contained SCC
			U.insert(choices.front().second.begin(), choices.front().second.end());
		}
		return U;
	}

	// Phase 2. The same step with backtracking: branch on every in-core reason
	// for one uncovered section, drop any set that has already reached the size
	// of the incumbent, and memoise on the set so a state is expanded once
	// however many ways the search reaches it. Running to completion proves the
	// incumbent is a MINIMUM self-contained subset of the SCC, and so has no
	// redundant subset, which is the hypothesis Theorem 3 needs. Reaching the cap
	// leaves the incumbent standing, self-contained but not certified minimal;
	// the cut is valid either way, only its generality is at stake.
	//
	// Measured over 3,265 cores: a median of 11 expansions and at worst 1,339,
	// and the search never improved on phase 1's incumbent, only proved it.
	void DeadlockAnalyzer::expand_core(
		ConflictGraph& graph, const set<int>& scc_set, CoreSearch& st, const set<int>& U) const
	{
		st.expansions++;
		if (st.expansions > st.cap) {
			st.capped = true;
			return;
		}
		if (st.visited.count(U) || U.size() >= st.best.size())
			return;
		st.visited.insert(U);

		int node, sec;
		if (!uncovered_section(graph, U, node, sec)) {
			if (U.size() >= 2)
				st.best = U;	// self-contained, and smaller than the incumbent
			return;
		}

		vector<pair<size_t, set<int>>> choices =
			in_core_choices(graph.node_sections.at(node)[sec], scc_set, U);
		for (auto& ch : choices) {
			if (st.capped)
				return;
			set<int> next = U;
			next.insert(ch.second.begin(), ch.second.end());
			expand_core(graph, scc_set, st, next);
		}
	}

	// Run both phases from the root-cause SCC and return the core the cut is
	// generated from. Phase 1 fixes an incumbent that is self-contained by
	// construction, so there is always a usable answer; phase 2 proves it minimum
	// or replaces it with something smaller.
	vector<int> DeadlockAnalyzer::prune_to_minimal_core(
		ConflictGraph& graph, const vector<int>& scc, vector<string>& pruned_cids)
	{
		if (scc.size() <= 2)
			return scc;		// a two-train SCC is already minimum

		set<int> scc_set(scc.begin(), scc.end());

		CoreSearch st;
		st.best = scc_set;
		st.cap = prune_search_cap;

		for (int seed : scc_set) {
			set<int> dive = dive_core(graph, scc_set, seed);
			if (dive.size() >= 2 && dive.size() < st.best.size())
				st.best = dive;
		}

		// PRUNE_SEARCH_CAP=0 runs phase 1 alone, which ablates the search while
		// leaving the pruning itself in place.
		for (int seed : scc_set) {
			if (st.cap <= 0 || st.capped)
				break;
			set<int> start;
			start.insert(seed);
			expand_core(graph, scc_set, st, start);
		}

		// The core is self-contained by construction, but this is the sole
		// correctness gate on the cut, so assert it rather than trust it.
		if (st.best.size() < 2 || !is_self_contained(graph, st.best))
			return scc;

		// Minimality is proven only where the search actually ran to completion.
		// PRUNE_SEARCH_CAP=0 disables it deliberately, which leaves the core
		// uncertified exactly as reaching the cap does, so it counts the same way.
		if (st.capped || st.cap <= 0) {
			search_capped++;
			if (st.capped && search_capped == 1)
				cout << "  NOTE: core search reached PRUNE_SEARCH_CAP=" << prune_search_cap
				     << "; core is self-contained but not certified minimal" << endl;
		} else {
			search_proven++;
		}

		for (int t : scc_set) {
			if (st.best.count(t))
				continue;
			Train* tr = graph.nodes[t]->train;
			pruned_cids.push_back(tr ? ("T" + to_string(tr->id)) : ("n" + to_string(t)));
		}
		if (st.capped)
			pruned_cids.push_back("CAP");
		trains_pruned += (long)(scc_set.size() - st.best.size());

		return vector<int>(st.best.begin(), st.best.end());
	}

	GeneratedConstraint DeadlockAnalyzer::generate_constraint_from_scc(
		ConflictGraph& graph, const vector<int>& scc)
	{
		GeneratedConstraint gc;
		gc.str_id = "dl_" + to_string(generated_constraint_counter++);

		set<int> scc_set(scc.begin(), scc.end());

		// For each member train, for each of its candidate sections, encode ONE
		// reason that lies entirely within the SCC (a singleton physical occupant,
		// or a constraint's tuple-trains). Encoding one reason per section — rather
		// than every blocker edge — keeps the cut tight and correct: a physical
		// section needs only one occupant present to stay blocked, so emitting all
		// occupants as a conjunction would wrongly demand them all.
		auto reason_in_scc = [&](const CandidateSection& cs) -> const BlockReason* {
			for (auto& r : cs.reasons) {
				bool all_in = true;
				for (auto& tr : r.trains)
					if (scc_set.find(tr.first) == scc_set.end()) { all_in = false; break; }
				if (all_in)
					return &r;
			}
			return nullptr;
		};

		// A5: collect, per blocker TRAIN, every segment it blocks with across the
		// retained reasons, then emit ONE footprint tuple per train. The old form
		// keyed on the blocking segment, so a train blocking with two segments
		// produced two tuples naming it twice (comment 2.12). Map keyed on node id,
		// so emission order is deterministic.
		map<int, set<int>> contributing_segs;	// blocker node -> segment ids it blocks with

		for (int u : scc) {
			auto it = graph.node_sections.find(u);
			if (it == graph.node_sections.end())
				continue;
			for (auto& cs : it->second) {
				const BlockReason* r = reason_in_scc(cs);
				if (r == nullptr)
					continue;	// self-contained guarantees one exists; skip defensively
				for (auto& [v, blocking_arc] : r->trains) {
					ConflictNode* blocker_node = graph.nodes[v];
					if (blocker_node->is_omega || blocker_node->train == nullptr)
						continue;
					contributing_segs[v].insert(blocking_arc->segment->id);
				}
				// The requester's own arcs that make this reason fire are load-bearing
				// too, so the footprint must reach back over them. Without this a
				// variant that reached the same head by another route would match the
				// cut without satisfying the reason, and the cut would deny a state
				// that is not doomed (Gap A). Arcs ahead of the head, inside the
				// requested section, are not in the train's current occupancy and are
				// dropped by the prefix walk below.
				for (auto a : r->self_arcs)
					contributing_segs[u].insert(a->segment->id);
			}
		}

		for (auto& [v, segs] : contributing_segs) {
			Train* blocker = graph.nodes[v]->train;

			// Occupancy head to tail: the locked arcs run outward from the train, so
			// locked.back() is the head boundary and reversing them walks back toward
			// the physical front; arcs then run front to tail.
			vector<DirectedSegment*> head_to_tail;
			for (auto rit = blocker->locked.rbegin(); rit != blocker->locked.rend(); ++rit)
				head_to_tail.push_back(*rit);
			for (auto a : blocker->arcs)
				head_to_tail.push_back(a);
			if (head_to_tail.empty())
				continue;

			// The footprint reaches back to the DEEPEST contributing arc and no
			// further: a longer prefix fires on strictly fewer states, a shorter one
			// could fire on states that are not doomed. Matched on segment rather
			// than arc identity, since a reason may record either orientation.
			int deepest = -1;
			for (int i = 0; i < (int)head_to_tail.size(); ++i)
				if (segs.count(head_to_tail[i]->segment->id))
					deepest = i;
			if (deepest < 0)
				continue;	// blocker no longer occupies what it blocked with; skip

			GeneratedConstraint::FootprintSpec spec;
			for (int i = 0; i <= deepest; ++i)
				spec.arcs.push_back({head_to_tail[i]->segment->id, head_to_tail[i]->head->id});
			spec.terminal_name = (blocker->target != nullptr) ? blocker->target->name : "NA";
			spec.min_length = blocker->length;
			gc.seg_specs.push_back(spec);
		}

		gc.occupancy_limit = max(1, (int)gc.seg_specs.size() - 1);
		return gc;
	}

	////////////////////////////////////////DeadlockMonitor

	DeadlockMonitor::DeadlockMonitor(Sim& _sim, SignalsManager* _signals_mgr, RailNetwork* _network,
		double _check_interval, double _deadlock_timeout)
		: SimObject("DeadlockMonitor", _sim),
		signals_mgr(_signals_mgr), network(_network),
		check_interval(_check_interval), deadlock_timeout(_deadlock_timeout),
		deadlock_detected(false), write_dot(false), verbose_dot(false), scc_log(nullptr), iteration(-1)
	{
	}

	SimCoroutine DeadlockMonitor::run()
	{
		while (true) {
			co_await delay(check_interval);

			// Drain: once past the warmdown point with a fully cleared network,
			// the [0, warmdown] cohort has escaped -> deadlock-free. Stop here so
			// safe seeds terminate promptly instead of idling to the big-M horizon.
			if (start_warmdown > 0 && ge(sim.time_now, start_warmdown, Train::eps)
				&& signals_mgr->active_trains.empty()) {
				sim.set_max_time(sim.time_now);
				co_return;
			}

			// Check if any access request has been waiting longer than the timeout
			bool timeout_found = false;
			for (auto req : signals_mgr->get_access_requests()) {
				if (sim.time_now - req->request_time > deadlock_timeout) {
					timeout_found = true;
					break;
				}
			}

			if (!timeout_found)
				continue;

			// A request has been waiting too long — run deadlock analysis
			DeadlockAnalyzer analyzer(signals_mgr, network);
			analyzer.write_dot = write_dot;
			analyzer.dot_path = dot_path;
			analyzer.scc_log = scc_log;
			analyzer.iteration = iteration;
			if (analyzer.analyze()) {
				// True deadlock detected
				deadlock_detected = true;
				generated_constraints = analyzer.get_generated_constraints();
				prune_log = analyzer.prune_log;

				cout << "[DeadlockMonitor] Deadlock detected at sim time " << sim.time_now
					<< " - generated " << generated_constraints.size() << " constraint(s)" << endl;

				// Signal simulation to stop by setting max_time to current time
				sim.set_max_time(sim.time_now);
				co_return;
			}
			// No deadlock SCC found -- dump conflict graph if verbose_dot enabled
			if (verbose_dot && dot_path.size() >= 4) {
				string verbose_path = dot_path.substr(0, dot_path.size() - 4)
					+ "_t" + to_string((int)sim.time_now) + ".dot";
				ConflictGraph vgraph;
				vgraph.build_from_state(signals_mgr, network);
				SCCResult vscc = tarjan_scc(vgraph.adjacency, (int)vgraph.nodes.size());
				write_conflict_graph_dot(vgraph, vscc, verbose_path, sim.time_now);
				cout << "[DeadlockMonitor] Verbose DOT: " << verbose_path
					<< " (" << vgraph.nodes.size() << " nodes, no deadlock SCC)" << endl;
			}
		}

		co_return;
	}

	////////////////////////////////////////Constraint application and export

	void apply_generated_constraints(RailNetwork* network, vector<GeneratedConstraint>& constraints,
		map<string, SignalConstraint*>* live_constraint_map)
	{
		for (auto& gc : constraints) {
			SignalConstraint* sc = new SignalConstraint(gc.str_id, network, gc.occupancy_limit);

			// Build seg_tuples from footprint specs and register on the front-signal arcs
			set<DirectedSegment*> registered_signals;
			for (auto& spec : gc.seg_specs) {
				if (spec.arcs.empty())
					throw runtime_error("Constraint " + gc.str_id + ": empty footprint");
				Terminal* target = nullptr;
				if (spec.terminal_name != "NA") {
					auto it = network->terminal_map.find(spec.terminal_name);
					if (it == network->terminal_map.end())
						throw runtime_error("Constraint " + gc.str_id + ": unknown terminal '"
							+ spec.terminal_name + "'");
					target = it->second;
				}

				SignalConstraint::SegTuple st;
				st.target = target;
				st.min_length = spec.min_length;
				for (auto& [seg_id, head_id] : spec.arcs) {
					// Validate IDs loaded from the constraint file against the current
					// network. If the network has changed since the file was written we
					// throw with context rather than reading out of bounds.
					if (seg_id < 0 || (size_t)seg_id >= network->segments.size())
						throw runtime_error("Constraint " + gc.str_id + ": footprint segment_id "
							+ to_string(seg_id) + " out of range [0, "
							+ to_string(network->segments.size()) + ")");
					if (head_id < 0 || (size_t)head_id >= network->nodes.size())
						throw runtime_error("Constraint " + gc.str_id + ": footprint head_node_id "
							+ to_string(head_id) + " out of range");
					DirectedSegment* arc = network->segments[seg_id]->get_arc(network->nodes[head_id]);
					if (!arc)
						throw runtime_error("Constraint " + gc.str_id + ": segment "
							+ to_string(seg_id) + " has no arc with head node " + to_string(head_id));
					st.arcs.push_back(arc);
				}
				sc->seg_tuples.push_back(st);

				// Register the constraint on each unique front-signal arc
				DirectedSegment* front_signal = st.front_signal();
				if (registered_signals.find(front_signal) == registered_signals.end()) {
					registered_signals.insert(front_signal);
					assert(front_signal->is_signal);
					front_signal->constraints.push_back(sc);
				}
			}

			if (live_constraint_map)
				(*live_constraint_map)[gc.str_id] = sc;
		}
	}

	// "seg:head>seg:head>..." head first. '>' and ':' keep the field comma-free
	// so the file stays a plain CSV.
	static string encode_footprint(const GeneratedConstraint::FootprintSpec& spec)
	{
		string out;
		for (auto& [seg_id, head_id] : spec.arcs) {
			if (!out.empty()) out += ">";
			out += to_string(seg_id) + ":" + to_string(head_id);
		}
		return out;
	}

	static void write_constraint_rows(ostream& f, GeneratedConstraint& gc)
	{
		f << gc.str_id << "," << gc.occupancy_limit << ",,\n";
		for (auto& spec : gc.seg_specs)
			f << gc.str_id << "," << encode_footprint(spec) << ","
			  << spec.terminal_name << "," << spec.min_length << "\n";
	}

	void write_generated_constraints(vector<GeneratedConstraint>& constraints, string path)
	{
		ofstream f(path);
		f << CONSTRAINT_FILE_HEADER << "\n";

		for (auto& gc : constraints)
			write_constraint_rows(f, gc);

		f.close();
	}

	vector<GeneratedConstraint> read_generated_constraints(string path, string last_constraint)
	{
		vector<GeneratedConstraint> constraints;
		vector<vector<string>> csv = read_csvf(path);

		// A5 replaced the segment-level tuple with the train-level footprint, and
		// footprints supersede it outright: there is no reason to keep reading the
		// old form, and reading it would mean guessing a route it never recorded.
		// Rejected by name rather than misinterpreted. The arc-based manual
		// constraints of signal_constraints.csv are a different mechanism and are
		// unaffected.
		// A header-only legacy file carries no tuples, so there is nothing to
		// misread and it is accepted as the empty set; some datasets ship one.
		if (csv.size() > 1 && csv[0].size() > 1 && csv[0][1] == "segment_id/occupancy_limit")
			throw runtime_error(path + " uses the pre-A5 segment-level tuple format, which "
				"footprint tuples have replaced. Regenerate it (for the engineered "
				"overlength set, generate_overlength_constraints.py now emits footprints) "
				"or retrain. Expected header: " + string(CONSTRAINT_FILE_HEADER));

		GeneratedConstraint* current = nullptr;

		for (int i = 1; i < (int)csv.size(); ++i) {
			csv_check::require_cols(csv, i, 2, path);
			string id = csv[i][0];

			// Check if this is a header row (terminal column is empty)
			if (csv[i].size() < 3 || csv[i][2].empty()) {
				// Check if we've passed the last_constraint cutoff
				if (last_constraint != "NA" && !constraints.empty() && constraints.back().str_id == last_constraint)
					break;

				constraints.push_back(GeneratedConstraint());
				current = &constraints.back();
				current->str_id = id;
				current->occupancy_limit = csv_check::parse_int(csv[i][1], path, i, "occupancy_limit");
			}
			else if (current != nullptr && current->str_id == id) {
				GeneratedConstraint::FootprintSpec spec;
				// Footprint: "seg:head>seg:head>...", head first.
				const string& fp = csv[i][1];
				size_t pos = 0;
				while (pos <= fp.size()) {
					size_t gt = fp.find('>', pos);
					string tok = fp.substr(pos, gt == string::npos ? string::npos : gt - pos);
					size_t colon = tok.find(':');
					if (colon == string::npos)
						throw runtime_error("Malformed footprint '" + fp + "' in " + path
							+ " row " + to_string(i + 1) + ": expected seg:head");
					spec.arcs.push_back({
						csv_check::parse_int(tok.substr(0, colon), path, i, "footprint segment_id"),
						csv_check::parse_int(tok.substr(colon + 1), path, i, "footprint head_node_id")});
					if (gt == string::npos)
						break;
					pos = gt + 1;
				}
				spec.terminal_name = csv[i][2];
				spec.min_length = (csv[i].size() > 3 && !csv[i][3].empty())
					? csv_check::parse_double(csv[i][3], path, i, "min_length") : 0.0;
				current->seg_specs.push_back(spec);
			}
		}

		return constraints;
	}

	////////////////////////////////////////Graph export for visualization

	void write_conflict_graph_dot(ConflictGraph& graph, SCCResult& scc, string path, double sim_time,
		const set<string>& pruned_constraint_ids, const set<int>& pruned_nodes)
	{
		// Build SCC membership: node_idx -> SCC index (only deadlock SCCs:
		// non-singleton, non-omega, no path to omega)
		map<int, int> node_scc;
		int scc_idx = 0;
		for (auto& component : scc.components) {
			if (component.size() <= 1)
				continue;
			bool has_omega = false;
			for (int idx : component)
				if (graph.nodes[idx]->is_omega) { has_omega = true; break; }
			if (has_omega)
				continue;

			// Check omega-reachability through external nodes
			bool reaches_omega = false;
			{
				queue<int> bfs;
				vector<bool> visited((int)graph.nodes.size(), false);
				for (int idx : component)
					visited[idx] = true;
				for (int idx : component) {
					for (int neighbor : graph.adjacency[idx]) {
						if (!visited[neighbor]) {
							visited[neighbor] = true;
							if (graph.nodes[neighbor]->is_omega) { reaches_omega = true; break; }
							bfs.push(neighbor);
						}
					}
					if (reaches_omega) break;
				}
				while (!bfs.empty() && !reaches_omega) {
					int curr = bfs.front();
					bfs.pop();
					for (int neighbor : graph.adjacency[curr]) {
						if (!visited[neighbor]) {
							visited[neighbor] = true;
							if (graph.nodes[neighbor]->is_omega) { reaches_omega = true; break; }
							bfs.push(neighbor);
						}
					}
				}
			}
			if (reaches_omega)
				continue;

			for (int idx : component)
				node_scc[idx] = scc_idx;
			scc_idx++;
		}

		// Colours for SCC clusters
		const char* scc_colours[] = { "lightsalmon", "lightblue", "lightgreen", "peachpuff", "plum", "wheat", "lightpink", "paleturquoise" };
		int n_colours = 8;

		ofstream f(path);
		if (!f.is_open()) {
			cerr << "ERROR: Could not open DOT file for writing: " << path << endl;
			return;
		}

		f << "digraph ConflictGraph {\n";
		f << "  dpi=200;\n";
		f << "  label=\"Conflict Graph at t=" << sim_time << "\";\n";
		f << "  labelloc=t;\n";
		f << "  fontsize=14;\n";
		f << "  node [shape=box, style=filled, fillcolor=white, fontsize=10, margin=\"0.2,0.15\"];\n";
		f << "  edge [fontsize=8];\n";
		f.flush();

		// Write nodes
		for (auto node : graph.nodes) {
			string label;
			string fillcolour = "white";
			string fontcolour = "black";

			if (node->is_omega) {
				label = "omega";
				fillcolour = "lightyellow";
			}
			else if (node->train != nullptr) {
				// Front arc str_id
				string front_arc_str = node->train->arcs.empty() ? "?" :
					(node->train->locked.empty() ? node->train->arcs.front()->str_id : node->train->locked.back()->str_id);
				string target_str = (node->target != nullptr) ? node->target->name : "NA";
				char len_buf[16];
				snprintf(len_buf, sizeof(len_buf), "%.2f", node->train->length);
				label = "T" + to_string(node->train->id) + " (" + string(len_buf) + "km)\\n" + front_arc_str + "\\n-> " + target_str;

				auto it = node_scc.find(node->id);
				if (it != node_scc.end()) {
					fillcolour = scc_colours[it->second % n_colours];
					fontcolour = "black";
				}
			}

			// Style pruned nodes with dashed border
		string node_style = "filled";
		if (pruned_nodes.count(node->id))
			node_style = "filled,dashed";
		f << "  " << node->id << " [label=\"" << label
		  << "\", style=\"" << node_style
		  << "\", fillcolor=\"" << fillcolour << "\", fontcolor=\"" << fontcolour << "\"];\n";
		}
		f.flush();

		// Write edges — pruned constraint edges drawn dashed and gray
		for (int i = 0; i < (int)graph.adjacency.size(); i++) {
			for (int j : graph.adjacency[i]) {
				auto it = graph.edge_labels.find({i, j});
				string combined;
				bool all_pruned = false;
				if (it != graph.edge_labels.end() && !it->second.empty()) {
					// Check if ALL labels on this edge are from pruned constraints
					all_pruned = !pruned_constraint_ids.empty();
					for (const string& lbl : it->second) {
						string cid = get_constraint_id(lbl);
						if (cid.empty() || pruned_constraint_ids.find(cid) == pruned_constraint_ids.end()) {
							all_pruned = false;
							break;
						}
					}
					for (int k = 0; k < (int)it->second.size(); k++) {
						if (k > 0) combined += "\\n";
						combined += it->second[k];
					}
				}

				if (all_pruned)
					f << "  " << i << " -> " << j << " [label=\"" << combined << "\", style=dashed, color=gray, fontcolor=gray];\n";
				else if (!combined.empty())
					f << "  " << i << " -> " << j << " [label=\"" << combined << "\"];\n";
				else
					f << "  " << i << " -> " << j << ";\n";
			}
		}

		f << "}\n";
		f.flush();
		f.close();
		cout << "DOT file written: " << path << " (" << graph.nodes.size() << " nodes)" << endl;
	}

	// Build a canonical geometry key for a constraint (sorted tuple descriptors, ignoring min_length)
	// Two specs describe the same train position: identical footprint, same
	// destination qualifier. A5's replacement for matching on (segment_id,
	// terminal, front_signal), which the footprint subsumes. Only ever called on
	// constraints that already share a geometry key, so this is a within-key
	// pairing of tuples, not a generality test.
	static bool same_footprint(const GeneratedConstraint::FootprintSpec& a,
		const GeneratedConstraint::FootprintSpec& b)
	{
		return a.arcs == b.arcs && a.terminal_name == b.terminal_name;
	}

	// Geometry key for duplicate detection: the sorted per-train footprints plus
	// destinations, ignoring min_length (which dominance orders separately).
	//
	// A5: this is the old key minus its separate segment_id field, because the
	// footprint already carries the blocking segments AND the front signal, whose
	// arc is simply arcs[0]. Two constraints share a key when they name the same
	// trains in the same places by the same routes.
	static string constraint_geometry_key(const GeneratedConstraint& gc) {
		vector<string> parts;
		for (auto& spec : gc.seg_specs) {
			string fp;
			for (auto& [seg_id, head_id] : spec.arcs)
				fp += to_string(seg_id) + ":" + to_string(head_id) + ">";
			parts.push_back(fp + spec.terminal_name);
		}
		sort(parts.begin(), parts.end());
		string key;
		for (auto& p : parts) {
			if (!key.empty()) key += "|";
			key += p;
		}
		return key;
	}

	////////////////////////////////////////Deadlock avoidance experiment

	void run_deadlock_avoidance_exp() {
		// Read experiment parameters from dlexp_options.csv
		int MAX_ITERATIONS = 500;
		double check_interval = 1.0;
		double deadlock_timeout = 2.0;
		int START_DEBUG = 501;
		string warm_start_file;
		string last_constraint = "NA";
		bool verify_seg = false;
		bool log_fires = false;
		bool merge_and_lower = true;
		bool scc_log_enabled = false;
		bool verbose_dot_enabled = false;
		bool pruning_enabled = true;	// PRUNING option; on unless explicitly "0"
		long prune_search_cap = 20000;	// PRUNE_SEARCH_CAP option; 0 runs phase 1 alone
		bool grant_timing = false;	// GRANT_TIMING option; per-decision timing, off by default

		vector<vector<string>> dlexp = read_csvf("./input/dlexp_options.csv");
		for (int i = 1; i < (int)dlexp.size(); ++i) {
			string label = dlexp[i][0];
			if (label == "MAX_ITERATIONS")
				MAX_ITERATIONS = stoi(dlexp[i][1]);
			else if (label == "DEFAULT_CHECK_INTERVAL")
				check_interval = stod(dlexp[i][1]);
			else if (label == "DEFAULT_DEADLOCK_TIMEOUT")
				deadlock_timeout = stod(dlexp[i][1]);
			else if (label == "START_DEBUG")
				START_DEBUG = stoi(dlexp[i][1]);
			else if (label == "WARM_START_FILE")
				warm_start_file = dlexp[i][1];
			else if (label == "LAST_CONSTRAINT")
				last_constraint = dlexp[i][1];
			else if (label == "VERIFY_SEG")
				verify_seg = (dlexp[i][1] == "1");
			else if (label == "LOG_CONSTRAINT_FIRES")
				log_fires = (dlexp[i][1] == "1");
			else if (label == "MERGE_AND_LOWER")
				merge_and_lower = (dlexp[i][1] == "1");
			else if (label == "SCC_LOG")
				scc_log_enabled = (dlexp[i][1] == "1");
			else if (label == "VERBOSE_DOT")
				verbose_dot_enabled = (dlexp[i][1] == "1");
			else if (label == "PRUNING")
				pruning_enabled = (dlexp[i][1] != "0");
			else if (label == "PRUNE_SEARCH_CAP")
				prune_search_cap = atol(dlexp[i][1].c_str());
			else if (label == "GRANT_TIMING")
				grant_timing = (dlexp[i][1] == "1");
			else if (label == "CONSTRAINT_EVAL") {
				string val = dlexp[i][1];
				if (val == "tree" || val == "verify")
					cout << "WARNING: CONSTRAINT_EVAL=" << val << " is no longer supported; using flat." << endl;
			}
		}

		SignalConstraint::verify_seg_mode = verify_seg;
		SignalConstraint::log_fires = log_fires;
		DeadlockAnalyzer::pruning_enabled = pruning_enabled;
		if (!pruning_enabled)
			cout << "PRUNING=0: pruning to a minimal self-contained core DISABLED (ablation)" << endl;
		DeadlockAnalyzer::prune_search_cap = prune_search_cap;
		GrantTiming::enabled = grant_timing;
		if (prune_search_cap <= 0)
			cout << "PRUNE_SEARCH_CAP=0: core search DISABLED (ablation); pruning keeps the phase 1 core" << endl;
		if (log_fires) {
#ifdef _MSC_VER
			fopen_s(&SignalConstraint::fire_log, "./output/constraint_fires.txt", "w");
#else
			SignalConstraint::fire_log = fopen("./output/constraint_fires.txt", "w");
#endif
			if (!SignalConstraint::fire_log)
				cerr << "Failed to open ./output/constraint_fires.txt for writing" << endl;
		}

		auto exp_start = chrono::high_resolution_clock::now();

		vector<GeneratedConstraint> all_constraints;  // learned constraints only
		vector<GeneratedConstraint> engineered_constraints;  // pre-cooked, kept separate
		set<string> engineered_geometry_keys;  // for skipping merge-and-lower
		map<string, SignalConstraint*> live_constraint_map;
		generated_constraint_counter = 0;
		int applied_constraint_count = 0;
		bool applied_engineered = false;

		// Create master once -- all config files read here, cached for reuse
		RailSimMaster master("./input/", "./output/");

		// Load engineered (pre-cooked) constraints from nogood_constr.csv if it exists
		{
			ifstream test("./input/nogood_constr.csv");
			if (test.good()) {
				test.close();
				engineered_constraints = read_generated_constraints("./input/nogood_constr.csv");
				for (auto& ec : engineered_constraints)
					engineered_geometry_keys.insert(constraint_geometry_key(ec));
				cout << "Engineered constraints: loaded " << engineered_constraints.size() << " from input/nogood_constr.csv" << endl;
			}
		}

		// Warm-start: load learned constraints from a previous run
		if (!warm_start_file.empty()) {
			all_constraints = read_generated_constraints(warm_start_file, last_constraint);
			cout << "Warm start: loaded " << all_constraints.size() << " constraints from " << warm_start_file << endl;
		}

		// Set generated_constraint_counter past any existing dl_ IDs in learned constraints
		for (auto& gc : all_constraints) {
			if (gc.str_id.substr(0, 3) == "dl_") {
				int id = stoi(gc.str_id.substr(3));
				if (id >= generated_constraint_counter)
					generated_constraint_counter = id + 1;
			}
		}

		// Open constraints file -- only learned constraints are written here
		ofstream constraints_file("./output/generated_constraints.csv");
		constraints_file << CONSTRAINT_FILE_HEADER << "\n";

		// Write warm-started learned constraints
		for (auto& gc : all_constraints)
			write_constraint_rows(constraints_file, gc);

		// Create experiment log file — remove from master_files so that
		// clear_master_logs() (called by network->reset()) doesn't close
		// the file handle and cause truncation on the next write.
		LogFile dlexp_log("dlexp_log", true, false, false);
		LogFile::master_files.erase("dlexp_log");
		dlexp_log.add("iteration", "int");
		dlexp_log.add("deadlock_found", "int");
		dlexp_log.add("deadlock_time", "double");
		dlexp_log.add("new_constraints", "int");
		dlexp_log.add("constraint_details", "string");
		dlexp_log.add("total_constraints", "int");
		dlexp_log.add("trains_completed", "int");
		dlexp_log.add("trains_per_hr", "double");
		dlexp_log.add("avg_wait_time", "double");
		dlexp_log.add("avg_spawn_delay", "double");
		dlexp_log.add("sim_run_time", "double");
		dlexp_log.add("pruning_details", "string");
		// 1 if the network fully cleared (drain complete / safe); 0 if trains
		// remained at the horizon (deadlock, or big-M anomaly under a drain).
		dlexp_log.add("drained_clear", "int");
		// 1 if the run stopped with trains active and requests pending while
		// generating nothing: a deadlock the learner could not express.
		dlexp_log.add("stalled", "int");
		// 1 if a drain was configured and the network had not cleared when the
		// run ended. 0 whenever no drain is configured.
		dlexp_log.add("run_incomplete", "int");

		// Per-decision grant timing (GRANT_TIMING=1). Its own file, so a run
		// without timing leaves dlexp_log.csv exactly as it was and no analysis
		// script sees a changed header. One row per iteration, which is also one
		// row per constraint-set size, since the set grows as the run proceeds.
		ofstream grant_timing_file;
		if (grant_timing) {
			grant_timing_file.open("./output/grant_timing.csv");
			grant_timing_file << "iteration,engineered_constraints,learned_constraints,total_constraints,"
				<< "decisions,constraint_evals,decision_mean_ns,decision_max_ns,"
				<< "eval_mean_ns,eval_total_ns,eval_max_ns,hist_log2_ns" << "\n";
			cout << "GRANT_TIMING=1: per-decision timing to ./output/grant_timing.csv" << endl;
		}

		// SCC debug log — opened once, appended across iterations
		ofstream scc_log_file;
		if (scc_log_enabled) {
			scc_log_file.open("./output/scc_log.csv");
			scc_log_file << "iteration,constraint_id,deadlock_time,scc_size,train,length,front_arc,target,edge_type,pre_prune_size,capped" << "\n";
		}

		for (int iteration = 0; iteration < MAX_ITERATIONS; iteration++) {
			cout << "\n=== Deadlock Avoidance Iteration " << iteration << " ===" << endl;
			cout << "Active constraints: " << engineered_constraints.size() << " engineered + " << all_constraints.size() << " learned" << endl;

			// 1. Reset simulation state (no file IO after first iteration)
			if (iteration > 0)
				master.reset();

			Train::train_counter = 0;

			// 2. Apply engineered constraints (once, on first iteration)
			if (!applied_engineered && !engineered_constraints.empty()) {
				apply_generated_constraints(master.network, engineered_constraints);
				applied_engineered = true;
			}

			// 2a. Apply only new learned constraints (previous ones persist on the network arcs)
			for (int c = applied_constraint_count; c < (int)all_constraints.size(); c++) {
				vector<GeneratedConstraint> single = { all_constraints[c] };
				apply_generated_constraints(master.network, single, &live_constraint_map);
			}
			applied_constraint_count = (int)all_constraints.size();

			// 3. Enable/disable debug outputs based on iteration
			bool debug_this_iter = (iteration >= START_DEBUG);
			Train::write_train_log = debug_this_iter;
			Train::write_metric_log = true;  // Always enable metric log for train completion counting
			master.animate = debug_this_iter && (master.script != nullptr);

			// Save sim_len before monitor potentially modifies max_time
			double sim_len = master.sim.max_time;

			// 4. Create and activate DeadlockMonitor
			string dot_file = "./output/conflict_graph_" + to_string(iteration) + ".dot";
			DeadlockMonitor* monitor = new DeadlockMonitor(
				master.sim, master.network->signals_mgr, master.network,
				check_interval, deadlock_timeout);
			monitor->write_dot = debug_this_iter;
			monitor->dot_path = dot_file;
			monitor->scc_log = scc_log_file.is_open() ? &scc_log_file : nullptr;
			monitor->iteration = iteration;
			monitor->verbose_dot = verbose_dot_enabled && debug_this_iter;
			monitor->start_warmdown = master.start_warmdown;
			monitor->activate();

			// 5. Run simulation
			if (grant_timing)
				GrantTiming::reset();
			LogFile* summary_log = master.run();

			// 5a. Per-decision grant timing for the run just finished, against the
			// constraint set it ran with. Written here rather than beside the
			// dlexp_log rows because those are split across the deadlock and
			// no-deadlock branches, and this measurement does not depend on which.
			if (grant_timing && grant_timing_file.is_open()) {
				long long n = GrantTiming::decisions;
				grant_timing_file << iteration << ","
					<< engineered_constraints.size() << ","
					<< applied_constraint_count << ","
					<< (engineered_constraints.size() + (size_t)applied_constraint_count) << ","
					<< n << "," << GrantTiming::evals << ","
					<< (n > 0 ? GrantTiming::decision_ns / n : 0) << ","
					<< GrantTiming::decision_max_ns << ","
					<< (n > 0 ? GrantTiming::eval_ns / n : 0) << ","
					<< GrantTiming::eval_ns << ","
					<< GrantTiming::eval_max_ns << ","
					<< GrantTiming::hist_str() << "\n";
				grant_timing_file.flush();
			}

			// 6. Check for deadlock
			bool deadlock_found = false;
			vector<GeneratedConstraint> new_constraints;
			vector<string> prune_details;

			if (monitor->deadlock_detected) {
				// Path (b): DeadlockMonitor detected local deadlock and halted sim
				deadlock_found = true;
				new_constraints = monitor->generated_constraints;
				prune_details = monitor->prune_log;
				cout << "Local deadlock detected by monitor at sim time." << endl;
			}
			else if (master.network->signals_mgr->is_deadlocked()) {
				// Path (a): Full stall — sim.run() returned with pending requests
				cout << "Full stall detected (event queue empty with pending requests)." << endl;
				DeadlockAnalyzer analyzer(master.network->signals_mgr, master.network);
				analyzer.write_dot = debug_this_iter;
				analyzer.dot_path = dot_file;
				analyzer.scc_log = scc_log_file.is_open() ? &scc_log_file : nullptr;
				analyzer.iteration = iteration;
				if (analyzer.analyze()) {
					deadlock_found = true;
					new_constraints = analyzer.get_generated_constraints();
					prune_details = analyzer.prune_log;
				}
			}

			// An unresolved stall: the run ended with trains active and requests
			// pending, but nothing was generated from it, either because the
			// monitor named no self-contained core or because the analyser
			// declined to name a cause. Such a run neither deadlocked in the
			// recorded sense nor drained, and the seed-level harness must be able
			// to tell it from a clean run, since a clean run is what the
			// stabilisation window counts. Evaluated before the monitor goes, so
			// the state it describes is the state the run stopped in.
			int stalled = master.network->signals_mgr->is_deadlocked() ? 1 : 0;

			// With a drain configured, a run that ends without the network
			// clearing did not complete: the [0, start_warmdown] cohort never all
			// escaped. Nothing circulates on these networks, so the realistic
			// cause is a train holding track with no outstanding access request,
			// which the monitor cannot see because it only inspects the request
			// queue, and which therefore trips neither the timeout nor the stall
			// test above. It is a tripwire for a bug rather than an expected
			// outcome. Always 0 when no drain is configured, so logs from
			// undrained runs are unaffected. Distinct from drained_clear, which
			// stays the raw fact and is read as-is by the drained held-out
			// analysis.
			int run_incomplete = (master.start_warmdown > 0
				&& !master.network->signals_mgr->active_trains.empty()) ? 1 : 0;

			// Clean up monitor before next iteration
			delete monitor;

			// 7. Collect metrics for log
			int train_count = summary_log ? summary_log->field("total_wait_time")->global_count : 0;
			// Rate denominator is the stats window, not the run end-time: under a
			// drain the run continues past warmdown, so time_now would understate it.
			double win_end = lfu::warmdown > 0 ? lfu::warmdown : master.sim.time_now;
			double collection_time = win_end - lfu::warmup;
			double trains_per_hr = collection_time > 0 ? (double)train_count / collection_time : 0;
			double avg_wait = train_count > 0 ? summary_log->field("total_wait_time")->global_tally / train_count : 0;
			double avg_spawn_delay = train_count > 0 ? summary_log->field("spawn_delay")->global_tally / train_count : 0;

			if (deadlock_found) {
				cout << "Generated " << new_constraints.size() << " new constraint(s):" << endl;
				string details;
				bool constraints_file_needs_rewrite = false;
				for (auto& gc : new_constraints) {
					// Check for geometry match with existing constraints (merge-and-lower)
					string new_key = constraint_geometry_key(gc);
					bool merged = false;
					// Skip merge-and-lower if geometry matches an engineered constraint
					if (engineered_geometry_keys.count(new_key)) {
						cout << "  " << gc.str_id << " geometry matches engineered constraint, creating new learned constraint" << endl;
					}
					else if (merge_and_lower) {
						for (auto& existing : all_constraints) {
							if (constraint_geometry_key(existing) == new_key) {
								// Dominance check: can we replace one with the other?
								// B dominates A if B.ml <= A.ml on ALL tuples (B fires whenever A fires)
								bool new_dominates_existing = true;  // gc dominates existing
								bool existing_dominates_new = true;  // existing dominates gc
								for (auto& existing_spec : existing.seg_specs) {
									for (auto& new_spec : gc.seg_specs) {
										if (same_footprint(existing_spec, new_spec)) {
											if (new_spec.min_length > existing_spec.min_length)
												new_dominates_existing = false;
											if (existing_spec.min_length > new_spec.min_length)
												existing_dominates_new = false;
											break;
										}
									}
								}
								if (existing_dominates_new) {
									// Existing is at least as general — discard new
									cout << "  " << gc.str_id << " dominated by " << existing.str_id
										<< " (discarded)" << endl;
									merged = true;
									break;
								}
								else if (new_dominates_existing) {
									// New is strictly more general — replace existing
									for (auto& existing_spec : existing.seg_specs) {
										for (auto& new_spec : gc.seg_specs) {
											if (same_footprint(existing_spec, new_spec)) {
												existing_spec.min_length = new_spec.min_length;
												break;
											}
										}
									}
									// Propagate to live constraint
									auto live_it = live_constraint_map.find(existing.str_id);
									if (live_it != live_constraint_map.end()) {
										SignalConstraint* live_sc = live_it->second;
										for (size_t si = 0; si < existing.seg_specs.size() && si < live_sc->seg_tuples.size(); si++)
											live_sc->seg_tuples[si].min_length = existing.seg_specs[si].min_length;
									}
									cout << "  " << gc.str_id << " dominates " << existing.str_id
										<< " (replaced thresholds)" << endl;
									merged = true;
									constraints_file_needs_rewrite = true;
									break;
								}
								// Neither dominates — keep both (no merge)
							}
						}
					}  // end merge-and-lower
					if (!merged) {
						cout << "  " << gc.str_id << " (segs="
							<< gc.seg_specs.size() << ")" << endl;
						all_constraints.push_back(gc);
					}
					if (!details.empty()) details += ";";
					details += gc.str_id + "(" + to_string(gc.seg_specs.size()) + ")" + (merged ? "M" : "");
				}

				// Write log record
				dlexp_log.data("iteration", iteration);
				dlexp_log.data("deadlock_found", 1);
				dlexp_log.data("deadlock_time", master.sim.time_now);
				dlexp_log.data("new_constraints", (int)new_constraints.size());
				dlexp_log.data("constraint_details", details);
				dlexp_log.data("total_constraints", (int)all_constraints.size());
				dlexp_log.data("trains_completed", train_count);
				dlexp_log.data("trains_per_hr", trains_per_hr);
				dlexp_log.data("avg_wait_time", avg_wait);
				dlexp_log.data("avg_spawn_delay", avg_spawn_delay);
				dlexp_log.data("sim_run_time", master.sim_run_time);
				string pruning_str;
				for (int i = 0; i < (int)prune_details.size(); i++) {
					if (i > 0) pruning_str += ";";
					pruning_str += prune_details[i];
				}
				dlexp_log.data("stalled", stalled);
				dlexp_log.data("run_incomplete", run_incomplete);
				dlexp_log.data("pruning_details", pruning_str);
				dlexp_log.data("drained_clear", 0);	// deadlock: network did not clear
				dlexp_log.write_current_record();

				// Write constraints to file
				if (constraints_file_needs_rewrite) {
					// Rewrite entire file (merge changed existing constraint thresholds)
					constraints_file.close();
					constraints_file.open("./output/generated_constraints.csv");
					constraints_file << CONSTRAINT_FILE_HEADER << "\n";
					for (auto& gc : all_constraints)
						write_constraint_rows(constraints_file, gc);
				}
				else {
					// Append new constraints only
					for (auto& gc : new_constraints)
						write_constraint_rows(constraints_file, gc);
				}
				constraints_file.flush();
			}
			else {
				// Simulation completed cleanly
				cout << "\nSimulation completed deadlock-free!" << endl;
				cout << train_count << " trains completed." << endl;
				cout << "Total constraints: " << engineered_constraints.size() << " engineered + " << all_constraints.size() << " learned" << endl;
				cout << "Iterations required: " << iteration + 1 << endl;
				cout << "Simulation run time: " << master.sim_run_time << " seconds." << endl;
				chrono::duration<double> exp_elapsed = chrono::high_resolution_clock::now() - exp_start;
				cout << "Total learning time: " << exp_elapsed.count() << " seconds." << endl;
				cout << "Pruning: " << DeadlockAnalyzer::search_proven << " cores proven minimal, "
					<< DeadlockAnalyzer::search_capped << " left unproven at PRUNE_SEARCH_CAP, "
					<< DeadlockAnalyzer::trains_pruned << " trains removed." << endl;

				// Write final log record
				dlexp_log.data("iteration", iteration);
				dlexp_log.data("deadlock_found", 0);
				dlexp_log.data("deadlock_time", master.sim.time_now);
				dlexp_log.data("new_constraints", 0);
				dlexp_log.data("constraint_details", string(""));
				dlexp_log.data("total_constraints", (int)all_constraints.size());
				dlexp_log.data("trains_completed", train_count);
				dlexp_log.data("trains_per_hr", trains_per_hr);
				dlexp_log.data("avg_wait_time", avg_wait);
				dlexp_log.data("avg_spawn_delay", avg_spawn_delay);
				dlexp_log.data("sim_run_time", master.sim_run_time);
				dlexp_log.data("stalled", stalled);
				dlexp_log.data("run_incomplete", run_incomplete);
				dlexp_log.data("pruning_details", string(""));
				// Under a drain, safe requires the network to have emptied; a
				// non-empty network here (reached big-M with no deadlock declared)
				// is an anomaly (livelock / undetected stall) the harness must catch.
				dlexp_log.data("drained_clear",
					master.network->signals_mgr->active_trains.empty() ? 1 : 0);
				dlexp_log.write_current_record();

				constraints_file.close();
				if (SignalConstraint::fire_log) { fclose(SignalConstraint::fire_log); SignalConstraint::fire_log = nullptr; }
				return;
			}
		}

		constraints_file.close();
		if (SignalConstraint::fire_log) { fclose(SignalConstraint::fire_log); SignalConstraint::fire_log = nullptr; }
		cout << "\nWARNING: Maximum iterations (" << MAX_ITERATIONS << ") reached without achieving deadlock-free simulation." << endl;
		chrono::duration<double> exp_elapsed = chrono::high_resolution_clock::now() - exp_start;
		cout << "Total learning time: " << exp_elapsed.count() << " seconds." << endl;
		cout << "Pruning: " << DeadlockAnalyzer::search_proven << " cores proven minimal, "
			<< DeadlockAnalyzer::search_capped << " left unproven at PRUNE_SEARCH_CAP, "
			<< DeadlockAnalyzer::trains_pruned << " trains removed." << endl;
	}

}//DesRailDL namespace
