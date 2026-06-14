#include "simulator.h"
#include <cassert>

/* String handling */
/*vector<string> split_string(const string& str, char delimiter) {
    vector<string> sub_str;
    stringstream stream(str);
    string _str;
    while (getline(stream, _str, delimiter)) sub_str.push_back(_str);
    return sub_str;
}*/

/*********************************************************/
/* --------------- COROUTINE CLASS --------------------- */
/*********************************************************/

SimCoroutine::SimCoroutine(coroutine_handle<promise_type> _handle) 
    : handle(_handle) {}


SimCoroutine::~SimCoroutine() {
}


void SimCoroutine::resume() { if (handle) handle.resume(); }
void SimCoroutine::activate() { resume(); }
coroutine_handle<SimCoroutine::promise_type> SimCoroutine::get_handle() { return handle; }

/*********************************************************/
/* --------------- COROUTINE PROMISE CLASS ------------- */
/*********************************************************/

SimPromise::SimPromise(SimObject& _obj) :obj(&_obj) {}
suspend_always SimPromise::initial_suspend() { return {}; }
//suspend_always SimPromise::final_suspend() noexcept { return {}; }
suspend_never SimPromise::final_suspend() noexcept { return std::suspend_never{}; }
void SimPromise::unhandled_exception() {}

void SimPromise::return_void()
{
}

SimCoroutine SimPromise::get_return_object() {
    coroutine_handle<SimPromise> handle = coroutine_handle<SimPromise>::from_promise(*this);
    return SimCoroutine{ handle };
}

/*********************************************************/
/*------------------ EVENTSTATE CLASS ------------------ */
/*********************************************************/

string EventState::to_string() const {
    switch (value) {
    case Values::PENDING: return "PENDING";
    case Values::TRIGGERED: return "TRIGGERED";
    case Values::PROCESSED: return "PROCESSED";
    case Values::ABORTED: return "ABORTED";
    default: return "UNKNOWN";
    }
}

/*********************************************************/
/*------------------- SIM EVENT CLASS ------------------ */
/*********************************************************/

SimEvent::SimEvent(Sim* _sim, BaseEventType* _type, SimObject* _obj)
    :sim(_sim), type(_type), obj(_obj), state(nullptr), handler(nullptr)
{
    id = sim->get_event_id();
    set_state(EventState::PENDING);

    //if (id == 557)
    //    int debug = 1;
}

//PC: added to remove memory leak
SimEvent::~SimEvent() {
    if (state != nullptr) delete state;
    //if (type != nullptr) delete type;
}

void SimEvent::set_state(EventState::Values value) { //PC: fixed memory leak
    if (state != nullptr) delete state;
    state = new EventState(value); 
}

void SimEvent::set_type(BaseEventType* _type) { type = _type; }

void SimEvent::log(ostream& os) const {
    if (type != nullptr and obj != nullptr) {
        //string text = format("EVENT<{}::{}>", type->to_string(), obj->descr);
        //TODO: gcc don't like format, will sort this out later
        //os << endl << text;
        ;
    }
}

bool SimEvent::is_pending() const { return ((*state)() == EventState::PENDING); }
bool SimEvent::is_triggered() const { return ((*state)() == EventState::TRIGGERED); }
bool SimEvent::is_processed() const { return ((*state)() == EventState::PROCESSED); }
bool SimEvent::is_aborted() const { return ((*state)() == EventState::ABORTED); }

// Add the resume method of a process as an handler of the event
// If the event is already triggered or aborted, nothing is done. 
bool SimEvent::add_handler(SimObject *_handler) {
    if (is_triggered()) return false; // Do nothing

    if (is_pending()) handler = _handler; // handlers.push_back(handler);
    return true;
}

// Process the event.
void SimEvent::process() {
    if (is_aborted() or is_processed()) return; // Do nothing

    if(handler != nullptr) handler->resume();

    set_state(EventState::PROCESSED); // Revise the event state
}

// Abort the event
bool SimEvent::abort() {
    if (!is_pending()) return false; // Do nothing
    
    set_state(EventState::ABORTED); // Change event state
    return true;
}

/*********************************************************/
/*--------------- SIM QUEUED EVENT CLASS --------------- */
/*********************************************************/

SimQueuedEvent::SimQueuedEvent(Sim* _sim,  BaseEventType* _type, SimObject* _obj, SimTime _time, int _priority)
    : SimEvent(_sim, _type, _obj), time(_time), priority(_priority) {}

// Trigger the scheduled event
bool SimQueuedEvent::trigger()
{
    if (!is_pending()) return false;

    if (time == sim->now()) set_state(EventState::TRIGGERED); // Change state from "PENDING"
    sim->queue_event(this); // Queue this event
    return true;
}

// Logging
void SimQueuedEvent::log(ostream& os) const {
    if (type != nullptr and obj != nullptr) //  Guard statements
    {
        //string text = format("[t={}]. EVENT<{}::{}>", time, type->to_string(), obj->descr);
        //os << endl << text;
        ;
        //gcc got issues with format, TODO: sort out later
    }
}

/*********************************************************/
/*------------ SIM CONDITIONAL EVENT CLASS ------------- */
/*********************************************************/

SimCondEvent::SimCondEvent(Sim* _sim, BaseEventType* _type, SimObject* _obj, SimObject* _handler)
    :SimEvent(_sim, _type, _obj), time_met(-1) { add_handler(_handler); }

/*********************************************************/
/*----------------- SIM CONDITION CLASS ---------------- */
/*********************************************************/

SimCondition& await_condition(SimCondition& condition) { return condition; }
std::suspend_always suspend_execution() { return std::suspend_always{}; }

SimCondition::SimCondition(Sim& _sim, ConditionFunc _func, bool _delete_after_trigger) 
    : sim(_sim), func(_func), delete_after_trigger(_delete_after_trigger) { sim.add_condition(this); }

bool SimCondition::is_met() { 
    if (handle)
        return func();
    else
        return true;    //this is to ensure handleless condition will be cleaned up (happens if func() evaluates to true in await_ready)
}

// Trigger a waiting coroutine
bool SimCondition::trigger() {
    if (is_met()) { // Triggering condition is met
        if (handle) handle.resume();// Resume the coroutine that is waiting
        return true;
    }  
    return false;
}

// Checks whether the coroutine should continue immediately or suspend.
bool SimCondition::await_ready() const { 
    bool result = func();
    //assert(!result);
    return result; }

// Activated when co_await is called to pause the coroutine
void SimCondition::await_suspend(coroutine_handle<SimPromise> _handle) { 
    handle = _handle; 
   // int debug = 1;
}

// Called when the coroutine resumes, after the awaited operation is completed.
void SimCondition::await_resume() {}

/*********************************************************/
/*----------------------- CLASS ------------------------ */
/*********************************************************/

Sim::Sim(int _id) : time_now(0), max_time(1440), id(_id) {
    next_event = nullptr; // Define no current event
    next_event_id = 0;
    system = nullptr;
}

void Sim::set_now(SimTime t) { time_now = t; }
SimTime Sim::now() { return time_now; }
int Sim::get_event_id() { return next_event_id++; }// Return current value and then increment
void Sim::set_max_time(SimTime _max_time) { max_time = _max_time; }
void Sim::queue_event(SimQueuedEvent *ev) { event_queue.push(ev); }
void Sim::dequeue_next_event() { event_queue.pop(); }
bool Sim::event_queue_empty() { return (event_queue.empty()); }

void Sim::reset() {
    // Delete remaining events in queue
    while (!event_queue.empty()) {
        delete event_queue.top();
        event_queue.pop();
    }

    // Delete pending terminated objects
    for (auto* o : obj_to_delete)
        delete o;
    obj_to_delete.clear();

    // Clear object list (objects managed by their owners, not by Sim)
    obj.clear();

    // Delete untriggered conditions (wait_until allocates these with new)
    for (auto* c : conditions)
        delete c;
    conditions.clear();

    // Clear conditional events
    cond_events.clear();

    // Reset time
    time_now = 0;
    next_event = nullptr;
}

void Sim::empty_event_queue() {
    while (!event_queue.empty()) event_queue.pop();
}

void Sim::schedule_event(SimTime t, BaseEventType *type, SimObject* ref, SimObject* handler, int priority) {
    SimQueuedEvent* event = new SimQueuedEvent(this, type, ref, t, priority);
    event->add_handler(handler);
    event->trigger();
}

//PC added for simplification
void Sim::schedule_event(SimTime t, SimObject* handler, int priority)
{
    schedule_event(t, nullptr, handler, handler, priority);
}

SimTime* Sim::peak_next_time() {
    return get_next_event() ? new SimTime(get_next_event()->time) : nullptr;
}

// This function will only identify queued_events;
// conditional events are invisible until they are triggered.
SimQueuedEvent* Sim::get_next_event() {
    return event_queue.empty() ? nullptr : event_queue.top();
}

void Sim::display_queued_events(ostream &os) {
    priority_queue<SimQueuedEvent*, vector<SimQueuedEvent*>, CompareSimQueuedEvent > copy_events;
    while (!event_queue.empty())
    {
        SimQueuedEvent* event = get_next_event();
        if (event) {
            os << endl << "Event: " << event->obj->descr;
            dequeue_next_event();
            copy_events.push(event);
        }
    }
    event_queue = copy_events;  // Reinstate events queue       
}

// Run the simulation until no scheduled events are left 
void Sim::run() { while (step()) {} }

bool Sim::advance_to(SimQueuedEvent &event) {
    while (event.is_pending() and !event_queue_empty()) step();
    return event.is_triggered();
 }

void Sim::advance_by(SimTime dur) {
    SimTime target = time_now + dur;
    while (!event_queue_empty()){
        SimTime next_time = *peak_next_time();
        if (next_time <= target) step(); 
        else break;
    }
    time_now = target;
}

// Check whether conditional events occur
void Sim::process_cond_events() { 
    //PC: replaced code below so these lists get cleaned up once triggered
    bool triggered = true;
    
    while (triggered) {
        triggered = false;
        for (auto it = conditions.begin(); it != conditions.end();) {
            SimCondition* condition = *it;
            if (condition->trigger()) {
                it = conditions.erase(it);
                if (condition->delete_after_trigger)
                    delete condition;
                triggered = true;
                break;
            }
            else
                ++it;
        }
    }//wend

    /*
    for (auto event : cond_events)
        event->trigger(); 
    for (auto condition : conditions) 
        condition->trigger();
    */
}

void Sim::add(SimObject* _obj) { obj.push_back(_obj); }
void Sim::add_cond_event(SimCondEvent *event) { cond_events.push_back(event); }
void Sim::add_condition(SimCondition* condition) { conditions.push_back(condition); }

void Sim::delete_terminated_objects()
{
    for (auto* obj : obj_to_delete)
        delete obj;
    obj_to_delete.clear();
}

void Sim::record_system_state() {
    if (system == nullptr) return;
    SystemState* sys_state = system->extract_state(time_now);
    system->history.add_state(sys_state); // Store current system state
}


// Activities during current simulation step 
bool Sim::step() {
    if (event_queue_empty()) {
        process_cond_events();  //attempt conditional events before giving up (this avoids potential issues of early termination at the very start of a simulation run)
        if (event_queue_empty())
            return false; // Do nothing
    }//if
    
    // ____________________________________________________
    // HANDLE SCHEDULED EVENTS
    // ----------------------------------------------------
    next_event = get_next_event(); // Extract next event from the event queue
    time_now = next_event->time; // Set current time as time of the event 

    if (time_now > max_time) {  //PC: added this
        time_now = max_time;
        return false;
    }//if

    next_event->process(); // Process the event
    dequeue_next_event(); // Remove event from the queue

    delete next_event;    //PC added: clean up memory leak
    next_event = nullptr;
    //delete next_event; // ?
    // 
    // _______________________________________________________
    // PROCESS TRIGGERED CONDITIONAL EVENTS 
    // ----------------------------------------------------
    process_cond_events(); 
    
    // ___________________________________________________
    // STATE RECORDING
    // ----------------------------------------------------
    record_system_state();
    
    // __________________________________________________

    //cleanup terminated objects
    delete_terminated_objects();
    return true;
}


/*********************************************************/
/* ----------------- SIMOBJECT CLASS ------------------- */
/*********************************************************/

SimObject::SimObject(string _descr, Sim& _sim)
    :sim(_sim), descr(_descr), state(nullptr), asleep(false) { sim.add(this); }

SimObject::~SimObject()
{
}

void SimObject::activate() {  
    handle = run().get_handle(); // Note: This cannot be done in the constructor
    // and must be done before the coroutine is started!
    resume(); // Method to activate coroutine
}

void SimObject::resume() { 
    handle.resume(); 
} // Resume coroutine

void SimObject::set_state(BaseObjState *_state) {
    state = _state;
}

//PC: added
std::suspend_always SimObject::delay(SimTime duration)
{
    sim.schedule_event(sim.time_now + duration, this);
    return suspend_execution();
}

//PC: added
std::suspend_always SimObject::sleep()
{
    assert(!asleep);
    asleep = true;
    return suspend_execution();
}

//PC: added
void SimObject::wakeup()
{
    //if (sim.time_now > 3.27834029095164)
    //    int debug = 1;

    if (!asleep) return;
    asleep = false;
    resume();
}

//PC: added
SimCondition& SimObject::wait_until(SimCondition::ConditionFunc func)
{
    SimCondition* condition = new SimCondition(sim, func, true);
    return *condition;
}

void SimObject::terminate()
{
    sim.obj.remove(this);
    sim.obj_to_delete.push_back(this);
}

/*********************************************************/
/* -------------------- STATE CLASS -------------------- */
/*********************************************************/

void SystemState::add(StateRecord &record) { 
    //records.insert({ record.first->descr, record }); // Optional
    update(record.first->descr, record);
}

void SystemState::update(string name, StateRecord& record) {
    records[name] = record;
}

void SystemState::log(ostream& os) {
    os << endl << "____________________________________________";
    os << endl << "System State @ t = " << time;
    os << endl << "--------------------------------------------";

    for (const auto & entry : records) { // entry.first is a string
        StateRecord record = entry.second;
        os << endl << record.first->descr << ": " << record.second->to_string();
        string info = get_more_info<string>(record.first->descr);
        if (info.length() > 0) os << " <" << info << ">";
    }
}

void SystemState::copy(SystemState& _parent) {
    time = _parent.time;
    //records.clear();
    records = _parent.records;
    //more_info.clear();
    more_info = _parent.more_info;
}

SystemState::StateRecord* SystemState::lookup_record(string name) { return &records[name]; }

std::any SystemState::lookp_info(string name) { return more_info[name]; }

/*********************************************************/
/* ---------------- STATE HISTORY CLASS ---------------- */
/*********************************************************/

SystemHistory::SystemHistory(System* _system):system(_system) {
}

void SystemHistory::add_state(SystemState* _state) { 
    states.push_back(_state); 
}

SystemState* SystemHistory::operator[](int index) { return states[index]; }

// Extract recorded state or else interpolate
SystemState* SystemHistory::get_state_at_time(SimTime t) {
    SystemState* nearest = find_nearest_state(t);
    if (nearest != nullptr) {
        if (nearest->time == t) return nearest;
        else return system->interpolate_state(t, nearest);
    }
    return nearest;
}


void SystemHistory::erase_states(SimTime from) {
    if (states.size() > 0) {
        while (states.back()->time > from) states.pop_back();
    }
}

SystemState* SystemHistory::find_nearest_state(SimTime t) {
    SystemState* nearest = nullptr;
    
    // Iterate in reverse: 
    for (auto it = states.rbegin(); it != states.rend(); ++it) {
        if ((*it)->time <= t) return (*it);
    }

    return nullptr; // Return last valid state or NULL
}

/*********************************************************/
/* ------------------- SYSTEM CLASS -------------------- */
/*********************************************************/

System::System(Sim& _sim) 
    :sim(_sim), history(this), state_zero(nullptr), current(nullptr) {
    sim.system = this;  // Backwards reference
}

void System::simulate() {
    sim.run();
}

// Extract current states 
SystemState* System::extract_state(SimTime t) {
    SystemState* _state = new SystemState(t);
    for (SimObject* _obj : sim.obj) { // Add a record for each SimObject
        SystemState::StateRecord record(_obj, _obj->state);
        _state->add(record);
    }
    return _state;
}


