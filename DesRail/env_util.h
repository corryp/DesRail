#pragma once

// `getenv` is deprecated under MSVC's secure-CRT rules (C4996) and this project builds warnings as
// errors, so the Windows build fails on it while g++ accepts it silently. These two helpers read
// the variable through whichever API the toolchain wants, which keeps the fix local instead of
// switching off C4996 project-wide -- the warning is worth having on the string functions it was
// really aimed at.
//
// Everything that uses these is a debug hook (`DESRAIL_TIME_INIT`, `DESRAIL_TRACE_SECTIONS`);
// nothing in a normal run depends on the environment.

#include <cstdlib>
#include <string>

inline bool env_value(const char* name, std::string& out)
{
#ifdef _MSC_VER
	char* buf = nullptr;
	size_t len = 0;
	if (_dupenv_s(&buf, &len, name) != 0 || buf == nullptr)
		return false;
	out.assign(buf);
	free(buf);
	return true;
#else
	const char* v = std::getenv(name);
	if (v == nullptr)
		return false;
	out.assign(v);
	return true;
#endif
}

inline bool env_is_set(const char* name)
{
	std::string ignored;
	return env_value(name, ignored);
}
