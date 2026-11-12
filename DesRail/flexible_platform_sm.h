#pragma once
#include <fstream>
#include "desrail.h"

namespace DesRail {

	// A SignalsManager that may substitute one platform for another at the last
	// signal before a station, without changing the timetable, the itinerary, or the
	// destination the rest of the engine works with.
	//
	// WHY IT IS SAFE TO LEAVE THE PRIMARY DESTINATION ALONE. The task keeps naming one
	// terminal until a substitution is actually granted, so the deadlock machinery, the
	// conflict graph and `reachable[]` all see a single-destination itinerary
	// throughout. Only the train that is physically being routed elsewhere, at the
	// moment it is routed, sees anything different.
	//
	// WHY THERE IS NO CLUSTER TABLE. A candidate must be a section that TERMINATES at a
	// terminal, and a section terminates at the FIRST terminal it reaches, so the
	// alternatives offered at any one signal are automatically faces of a single
	// station. Measured on the inner-Brisbane network: of the 27 signals where more
	// than one section terminates at a terminal, none spans two stations. A platform
	// that is merely reachable further downstream (Milton, seen from the western
	// approach to Roma Street) is not a candidate, because the section that reaches it
	// does not end there.
	//
	// THE GUARD, in the order the checks are made:
	//   1. the booked platform always wins when it is available, whatever the priorities
	//      say -- ordering the alternatives is arc priority's job, choosing between the
	//      booked one and an alternative is not;
	//   2. the candidate must terminate at a terminal, and not at the booked one;
	//   3. there must BE a next call. On the last task the onward guard below is
	//      vacuous, and the network boundary is exactly where terminals fan out
	//      together, so an unguarded last task lets a service leave by the wrong line;
	//   4. the candidate must not BE the next call, or a train would skip a stop by
	//      running straight to where it was going next;
	//   5. this must be the last signal before the booked platform, i.e. some section
	//      from here terminates at it. Without this a train could be diverted to another
	//      station entirely from a signal kilometres away;
	//   6. the next call must be reachable from the BOOKED platform. If it is not, the
	//      train is about to reverse (that is precisely what makes maybe_reverse turn it
	//      around), and a reversing service must not be rerouted: it holds a platform
	//      for minutes, and a substitution could silently leave it not reversing, which
	//      changes the service pattern rather than the platform;
	//   7. the next call must be reachable from the CANDIDATE, or the train is stranded.
	//      Reachability is not transitive here -- a sub-search stops at the first
	//      terminal it meets -- which is exactly the question being asked: can this
	//      train run its next leg without calling somewhere else first.
	//
	// NOT COMPATIBLE WITH THE LEARNED CONSTRAINTS. Constraint evaluation matches on
	// `train->target` and `task->terminal`, both of which this manager can change
	// mid-journey, so a conflict graph built on fixed destinations is no longer sound.
	// Run it only on scenarios with no nogood_constr and no signal_constraints rows.
	class FlexiblePlatformSignalsManager : public SignalsManager {
	public:
		FlexiblePlatformSignalsManager(Sim& _sim, RailNetwork* _network);
		~FlexiblePlatformSignalsManager() override;

		int substitutions() const { return n_substitutions; }

	protected:
		bool section_serves(TrackSection* section, Terminal* target, Train* train,
			DirectedSegment* from_arc, Terminal*& matched) const override;
		void on_granted(Train* train, TrackSection* section, Terminal* target, Terminal* matched) override;

	private:
		// The index of the task a request for `target` serves: the current task, or the
		// next one when the train is passing through a zero-dwell call and has requested
		// ahead of advancing. -1 if neither names `target`.
		int served_task(Train* train, Terminal* target) const;
		// The call after the one the request serves, or nullptr if there is none.
		Terminal* next_call(Train* train, Terminal* target) const;
		// The section from `from_arc` that TERMINATES at `target`, or nullptr. Non-null
		// is the definition of "at the last signal before the destination".
		TrackSection* terminating_section(DirectedSegment* from_arc, Terminal* target) const;

		// ofstream rather than FILE*: MSVC's SDL check promotes the fopen deprecation
		// (C4996) to an error, and suppressing it project-wide to keep a C idiom would
		// be the wrong trade.
		ofstream log;
		int n_substitutions;
	};

}//DesRail namespace
