#include <iomanip>
#include "flexible_platform_sm.h"

namespace DesRail {

	FlexiblePlatformSignalsManager::FlexiblePlatformSignalsManager(Sim& _sim, RailNetwork* _network) :
		SignalsManager(_sim, _network), n_substitutions(0)
	{
		// Same convention as the constraint-fire log: relative to the run workspace.
		log.open("./output/platform_reassignments.csv");
		if (log)
			log << "time,train,service,task,booked,actual\n";
	}

	FlexiblePlatformSignalsManager::~FlexiblePlatformSignalsManager()
	{
		if (log.is_open())
			log.close();
	}

	int FlexiblePlatformSignalsManager::served_task(Train* train, Terminal* target) const
	{
		if (train == nullptr || train->job == nullptr || train->task_idx < 0)
			return -1;
		auto& tasks = train->job->tasks;
		for (size_t k = (size_t)train->task_idx; k < tasks.size() && k <= (size_t)train->task_idx + 1; k++) {
			if (tasks[k]->terminal == target)
				return (int)k;
		}
		return -1;
	}

	Terminal* FlexiblePlatformSignalsManager::next_call(Train* train, Terminal* target) const
	{
		int k = served_task(train, target);
		if (k < 0)
			return nullptr;
		size_t nxt = (size_t)k + 1;
		if (nxt >= train->job->tasks.size())
			return nullptr;
		return train->job->tasks[nxt]->terminal;
	}

	TrackSection* FlexiblePlatformSignalsManager::terminating_section(
		DirectedSegment* from_arc, Terminal* target) const
	{
		for (auto s : from_arc->out_sections_ranked) {
			if (s->terminal == target)
				return s;
		}
		return nullptr;
	}

	bool FlexiblePlatformSignalsManager::section_serves(TrackSection* section, Terminal* target,
		Train* train, DirectedSegment* from_arc, Terminal*& matched) const
	{
		// 1. the booked platform, unchanged. This is also the hot path: every request
		// that is not a substitution costs one array lookup, exactly as before.
		if (section->reachable[target->id]) {
			matched = target;
			return true;
		}

		// 2-4. cheap per-candidate tests first, so the scan in 5 is rarely reached.
		Terminal* alt = section->terminal;
		if (alt == nullptr || alt == target)
			return false;
		Terminal* next = next_call(train, target);
		if (next == nullptr || alt == next)
			return false;

		// 5. are we at the last signal before the booked platform?
		TrackSection* primary = terminating_section(from_arc, target);
		if (primary == nullptr)
			return false;

		// 6. a train that cannot reach its next call from the BOOKED platform is about
		// to reverse; leave it alone.
		if (!network->can_reach(primary->arcs.back(), next))
			return false;

		// 7. and it must be able to run its next leg from the candidate.
		if (!network->can_reach(section->arcs.back(), next))
			return false;

		matched = alt;
		return true;
	}

	void FlexiblePlatformSignalsManager::on_granted(Train* train, TrackSection* section, Terminal* target, Terminal* matched)
	{
		if (matched == nullptr || matched == target)
			return;		//the booked platform was granted: nothing to do
		// Retarget the task the request was FOR, not blindly the current one: a train
		// passing through a zero-dwell call asks ahead for the next task's platform.
		int k = served_task(train, target);
		if (k < 0)
			return;
		Task* task = train->job->tasks[k];

		if (log) {
			Terminal* booked = task->booked ? task->booked : task->terminal;
			log << fixed << setprecision(6) << sim.time_now << "," << train->id << ","
				<< train->job->service_name << "," << k << ","
				<< booked->name << "," << matched->name << "\n";
		}
		n_substitutions++;

		// Retarget. Both matter: Train::run exits its per-task loop on
		// `arcs.front()->terminal == task->terminal`, and `target` is what the next
		// request_access asks for. `booked` is untouched, so the comparison survives.
		task->terminal = matched;
		if (k == train->task_idx)
			train->target = matched;
	}

}//DesRail namespace
