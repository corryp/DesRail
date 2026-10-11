#include "desrail.h"
#include "flexible_platform_sm.h"

// Resolution of the `signals_manager` general parameter to a concrete manager.
//
// This file exists so that the core (desrail.cpp) never mentions a routing policy.
// It is the one place that includes the policy headers, so a new policy is added by
// including it here and adding one branch, and removed by deleting both -- the engine
// itself is untouched either way.
//
// An unrecognised name is a hard error. A scenario that asks for a policy the build
// does not have must fail loudly: silently falling back to "base" would turn an
// experiment rung into an unlabelled duplicate of its own control, and the result
// would read as a null finding rather than as a misconfiguration.

namespace DesRail {

	SignalsManager* make_signals_manager(Sim& sim, RailNetwork* network)
	{
		const string& name = network->signals_manager;

		if (name.empty() || name == "base")
			return new SignalsManager(sim, network);

		if (name == "flexible_platform")
			return new FlexiblePlatformSignalsManager(sim, network);

		throw runtime_error("unknown signals_manager '" + name + "' in general_params.csv");
	}

}//DesRail namespace
