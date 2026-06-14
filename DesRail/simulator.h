
#pragma once

#include <iostream>
#include <fstream>
#include <queue>
#include <thread>
#include <coroutine>
#include <atomic>
#include <chrono>
#include <functional>
#include <vector>
#include <memory>  // For std::unique_ptr
#include <list>
#include <set>
#include <random>
#include <queue>
//#include <format>
#include <algorithm>
#include <any>  // for std::any
#include <cassert>
using namespace std;

/* String handling */
//vector<string> split_string(const string& str, char delimiter);

/*vector<string> split_string(const string& str, char delimiter) {
    vector<string> sub_str;
    stringstream stream(str);
    string _str;
    while (getline(stream, _str, delimiter)) sub_str.push_back(_str);
    return sub_str;
}*/

/*********************************************************/
/* ---------------- BACKGROUND THEORY ------------------ */
/*********************************************************/

/*
    Processes generate and respond to events, influencing the system's 
    state and scheduling new events.
    
    Each process handles its own events and schedules future events 
    based on interactions with other processes.

    When modelling the following must be determined:
    1) Which SimObject are passive and which are active?
    2) What does each SimObject do?
    3) What events must be explicitly modelled
    4) How to ensure progression of simulation time
*/

/*********************************************************/
/* ------------------ REGARDING STATES ----------------- */
/*********************************************************/

/*
    EVENT STATE::PENDING: An event that has not been triggered or aborted
    EVENT STATE::TRIGGERED: An event that has occurred, but not processed
    EVENT STATE:PROCESSED: An event that has been triggered and processed.
    EVENT STATE::ABORTED: An event that has been aborted.

    States: A triggered event refers to an event that is scheduled or initiated 
    based on certain conditions or actions within the simulation. In other words,
    it is an event that has been set to occur at a specific point in time due 
    to some prior action or condition.
    
    A processed event is an event that has already occurred and has been handled or 
    executed by the simulation system. Once an event is processed, it has been acted 
    upon and its effects have been incorporated into the simulation. 

*/

/*********************************************************/
/* ------------------ REGARDING EVENTS ----------------- */
/*********************************************************/

/*
    The rationale of discrete event simulation is to schedule 
    events to occur at future times. For example, when a task 
    needs to occur, a task commencement event is scheduled. 

    Conditional events, are different. Whilst known, the time 
    they occur is unknown. They are triggered conditionally. 
    At that point, a conditional event may become a scheduled event.
*/

/*********************************************************/
/* --------------- ENUMERATED TYPE CLASS --------------- */
/*********************************************************/

class EventState
{
public:
    enum Values { PENDING, TRIGGERED, PROCESSED, ABORTED };
    EventState(Values _value) :value(_value) {}
    Values get() const { return value; }
    Values operator()() const { return value; }
    string to_string() const;
private:
    Values value;
};

/*********************************************************/
/* ------------- ABSTRACT ENUMERATED TYPE -------------- */
/*********************************************************/

class BaseObjState
{
public:
    virtual ~BaseObjState() {} // Virtual destructor
    virtual string to_string() const = 0; // Pure virtual method
    virtual int get() const = 0; // Pure virtual method
};

/*********************************************************/
/* ------------- ABSTRACT ENUMERATED TYPE -------------- */
/*********************************************************/

class BaseEventType
{
public:
    virtual ~BaseEventType() {} // Virtual destructor
    virtual string to_string() const = 0; // Pure virtual method
    virtual int get() const = 0; // Pure virtual method
};

/*********************************************************/
/* --------------- FORWARD DECLARATRIONS --------------- */
/*********************************************************/

using SimTime = double;  // (a.k.a., SimTimeUnit)

class Sim;  //  Simulator (a.k.a., Simulation)
class SimEvent;
class SimObject;   // (a.k.a., SimProcess)
class SimPromise;
class SimCoroutine;
class SimCondEvent;
class SystemState;
class System;
class SystemHistory;
class Actions;

/*********************************************************/
/* --------------- COROUTINE CLASS --------------------- */
/*********************************************************/

/*
    SimCoroutine — return type of SimObject::run().

    Wraps a std::coroutine_handle<SimPromise>. The handle is the entry point
    for resuming the coroutine when its scheduled event fires. The promise
    type is fixed (SimPromise) and uses suspend_never on final_suspend, so the
    coroutine frame outlives the natural completion of run() and is freed in
    bulk by Sim::reset() rather than per-completion. See SimPromise for why.
*/

class SimCoroutine {
public:
    using promise_type = SimPromise;

    std::coroutine_handle<promise_type> handle;  // handle of type 'promise_type'
    SimCoroutine(coroutine_handle<promise_type> _handle);
    ~SimCoroutine();
    std::coroutine_handle<promise_type> get_handle();
    void resume();
    void activate();
};

/*********************************************************/
/* --------------- COROUTINE PROMISE CLASS ------------- */
/*********************************************************/

/*
    SimPromise — coroutine promise type for SimObject::run().

    Two design choices worth knowing:
    - final_suspend() returns suspend_never, NOT suspend_always. This means
      the coroutine frame is *not* destroyed automatically when run() returns
      normally. Frames are explicitly cleared by Sim::reset() between
      experiment iterations. This lets the engine inspect or retain frame
      state after run() completes.
    - The promise stores a SimObject* back-reference so the engine can resume
      the right object's coroutine when its event fires. Lifetime is tied to
      the SimObject — if the object is deleted, the promise pointer dangles.
*/

class SimPromise
{
public:
    /* Attribute */
    SimObject* obj;
    /*
        Note: A reference to the SimObject is necessary to ensure that the coroutine's promise_type 
        is tied to the lifetime of the actual object. The coroutine depends on the existence of
        the object outside of the coroutine itself, and on the state of that object. Otherwise 
        copying the object might cause issues, if the original object gets destroyed or modified.
        
        UNTESTED: It may be better to hold a coroutine handle to manage the coroutine's execution
        std::coroutine_handle<> handle;
    */
 
    /* Function */
    SimPromise() = default;
    SimPromise(SimObject& _obj);
    SimCoroutine get_return_object(); // Creates a new Coroutine object by initializing it with a handle to the coroutine's state.
    suspend_always initial_suspend();
    //suspend_always final_suspend() noexcept;
    suspend_never final_suspend() noexcept;
    void unhandled_exception();
    
    // Define either return_void or return_value, but not both.
    void return_void();
};


/*********************************************************/
/*----------------------- CLASS ------------------------ */
/*********************************************************/

/* The core Simulation Event object */

class SimEvent {
public:
    /* Attribute */
    int id;
    BaseEventType* type; // Type of event that will occur - application specific
    SimObject* obj; // SimObject related to the event
    EventState* state; // The state of the event
    Sim* sim; // Reference to the simulation engine
    SimObject* handler; // Object associated with the event

    // vector<SimObject*> handlers; // Unused
  
    /* Function */
    SimEvent(Sim* _sim, BaseEventType* _type, SimObject *_obj);
    ~SimEvent();    //PC: added to remove memory leak
    virtual void log(ostream& os) const;
    void set_state(EventState::Values value);
    void set_type(BaseEventType* _type);
    void process();
    bool abort(); 
    bool add_handler(SimObject* handler);  
    bool is_pending() const;
    bool is_triggered() const;
    bool is_processed() const;
    bool is_aborted() const;
};

/*********************************************************/
/*----------------------- CLASS ------------------------ */
/*********************************************************/

/* An event queued (a.k.a., scheduled) for a future time */

class SimQueuedEvent : public SimEvent
{
public:
    /* Attribute */
    SimTime time; // Event is scheduled to occur at this time
    int priority;

    /* Function */
    SimQueuedEvent(Sim* _sim,  BaseEventType* _type, SimObject* _obj, SimTime _time, int _priority = 0);
    bool trigger();
    void log(ostream& os) const override;
};

/*********************************************************/
/*----------------------- CLASS ------------------------ */
/*********************************************************/

class SimCondEvent : public SimEvent {
public:
    SimTime time_met;
    SimCondEvent(Sim* _sim, BaseEventType* _type=NULL, SimObject* _obj=NULL, SimObject* _handler=NULL);
    virtual void trigger() = 0; 
};

/*********************************************************/
/*----------------------- CLASS ------------------------ */
/*********************************************************/

class SimCondition {
public:
    using ConditionFunc = std::function<bool()>;

    /* Attribute */
    ConditionFunc func; // Function specifying condition logic
    coroutine_handle<SimPromise> handle; // Handle to coroutine
    Sim &sim; // Reference to the simulation engine
    bool delete_after_trigger;  //PC: added to avoid memory leek for behind the scenes management of conditions
 
    /* Functions */
    SimCondition(Sim& _sim, ConditionFunc _func, bool _delete_after_trigger= false);
    bool is_met();
    bool trigger();
    bool await_ready() const;
    void await_suspend(coroutine_handle<SimPromise> _handle);
    void await_resume();
};

SimCondition& await_condition(SimCondition& condition);

std::suspend_always suspend_execution();


/*********************************************************/
/*----------------------- CLASS ------------------------ */
/*********************************************************/

/* class for the comparison of SimQueuedEvent objects  */

class CompareSimQueuedEvent {
public:
    CompareSimQueuedEvent() {}

    bool operator()(const SimQueuedEvent* ev1, const SimQueuedEvent* ev2) {
        if (ev1->time == ev2->time) return (ev1->priority < ev2->priority); // Descending order w.r.t. priority
        else return (ev1->time > ev2->time); //  Ascending order w.r.t. time
    }
};


/*********************************************************/
/*------------------ SIMULATION CLASS ------------------ */
/*********************************************************/

/*
    Sim — the discrete-event simulation engine.

    Owns the global event queue (priority_queue ordered by time, then by
    priority), the list of pending conditional events, and the current
    simulation time. run() repeatedly calls step(), which pops the next
    event, advances time_now, and resumes the associated coroutine. The
    loop terminates when the queue is empty or time_now >= max_time.

    Two awaitables are exposed indirectly via SimObject::delay/sleep/
    wait_until: a delay schedules a future event and suspends; sleep
    suspends without scheduling (an external waker like a coroutine
    completion or a triggered condition resumes it).

    reset() clears all dynamic state — events, conditions, objects,
    coroutine frames — for reuse. The deadlock-avoidance learner relies on
    this to re-run with a different constraint set without rebuilding the
    network from CSV each time.

    The engine itself is domain-agnostic: it knows nothing about trains,
    tracks, or rails. The same machinery could drive any DES.
*/

class Sim {
public:
    using EventQueue = priority_queue<SimQueuedEvent*, vector<SimQueuedEvent*>, CompareSimQueuedEvent >;
   
    /* Attribute */
    System* system;
    list<SimObject*> obj;
    list<SimObject*> obj_to_delete;
    EventQueue event_queue; // Scheduled events
    list<SimCondEvent*> cond_events;
    list<SimCondition*> conditions;
    SimQueuedEvent* next_event;
    SimTime time_now;// current simulation time
    SimTime max_time; // time limit
    int next_event_id, id;


    /* Function */
    Sim(int _id); 
    SimTime now();
    int get_event_id();
    void set_max_time(SimTime _max_time);
    void run();
    void set_now(SimTime t);
    void reset();	//clears all dynamic state for reuse
    void empty_event_queue();
    void advance_by(SimTime dur);
    bool advance_to(SimQueuedEvent& event);
    void queue_event(SimQueuedEvent *event);
    void schedule_event(SimTime t, BaseEventType *type, SimObject *ref, SimObject *handler, int priority=0);
    void schedule_event(SimTime t, SimObject* handler, int priority = 0);   //PC: added to offer a simplified event scheduling
    void process_event(SimEvent& event);
    void dequeue_next_event();
    void display_queued_events(ostream &os);
    bool event_queue_empty();
    bool step();
    void record_system_state();
    void copy_queue(list<SimObject*>& lst);
    void process_cond_events();
    void add(SimObject* _obj);
    void add_cond_event(SimCondEvent* event);
    void add_condition(SimCondition* condition);
    void delete_terminated_objects();
    SimQueuedEvent* get_next_event();
    SimTime* peak_next_time();
};

/*********************************************************/
/* ------------------ SIMOBJECT CLASS ------------------- */
/*********************************************************/

/*
    SimObject — base class for any process in the simulation.

    Subclasses override run() to define their behaviour. Inside run(),
    suspending the coroutine is done via three awaitables:
      - co_await delay(t)          — sleep for t simulation units
      - co_await sleep()           — suspend until externally woken
      - co_await wait_until(pred)  — suspend until a predicate is true

    Any local variable declared in run() that needs to persist across a
    co_await is automatically stored in the coroutine frame by the compiler
    — no special handling required.

    Lifetime: a SimObject's coroutine frame is *not* freed when run()
    returns. The handle and frame live until the SimObject itself is
    deleted (typically in Sim::reset()) or until Sim's coroutine cleanup
    runs. Be careful capturing references (especially `[this]`) into
    wait_until lambdas: if the object is destroyed before the condition
    fires, the captured `this` dangles. In normal usage SimObjects survive
    long enough that this is fine.
*/

class SimObject {
public:
    using Handle = coroutine_handle<SimPromise>;

    /* Attribute */
    std::string descr;  // Object description
    Sim& sim; // Reference to the simulator
    BaseObjState* state; // Dynamic & application specific
    Handle handle; // Storing "run" coroutine handle
    bool asleep;    //PC: added to support sleep() - indicates object is suspended by call to sleep
    
    /* Function */
    SimObject(string _descr, Sim& _sim);
    virtual ~SimObject();
    void set_state(BaseObjState *_state);
    void activate(); // Method to activate coroutine
    void resume();
    std::suspend_always delay(SimTime duration);    //PC added this for single line delay of processes
    std::suspend_always sleep();                    //PC added this to safely suspend and resume without risk of premature resume
    void wakeup();                                  //PC added (resumes if asleep)
    SimCondition& wait_until(SimCondition::ConditionFunc _func);    //PC added for single line conditional waits
    virtual SimCoroutine run() = 0; // Pure virtual function
    void terminate();
};


/*********************************************************/
/* -------------------- STATE CLASS -------------------- */
/*********************************************************/

/* State of the system at a specific time */

class SystemState {
public:
    using StateRecord = pair<SimObject*, BaseObjState*>;

    /* Attribute */
    SimTime time;
    std::unordered_map<std::string, StateRecord> records;

    // Map to store additional data with a string key
    std::unordered_map<std::string, std::any> more_info;

    /* Function */
    SystemState(SimTime _time) :time(_time) {}
    void update(string name, StateRecord& record);
    void add(StateRecord &record);
    void log(ostream& os);
    StateRecord* lookup_record(string name);
    std::any lookp_info(string name);
    void copy(SystemState& _parent);
     
    template <typename T>
    void add_more_info(string& key, T value) { more_info[key] = value; }

    template <typename T>
    T get_more_info(const string& key) {
        if (more_info.find(key) != more_info.end())
            return std::any_cast<T>(more_info.at(key));
        else return T();
    }

};

/*********************************************************/
/* --------------- STATE HISTORY CLASS ----------------- */
/*********************************************************/

/* Chronology of System states */

class SystemHistory {
public:
    /* Attribute */
    vector<SystemState*> states;
    System* system;

    /* Function */
    SystemHistory(System* _system=NULL);
    void add_state(SystemState* _state);
    void erase_states(SimTime from);

    SystemState* find_nearest_state(SimTime t);
    SystemState* operator[](int index);
    SystemState* get_state_at_time(SimTime t);
    SystemState* get_last() { return states.back(); }
};

/*********************************************************/
/* ------------------- ACTION CLASS -------------------- */
/*********************************************************/

typedef std::any Action;

class Actions {
public:
    /* Attribute */
    vector<Action> info;

    /* Function */
    Actions() {}
    void clear() { info.clear(); }
    int size() { return (int) info.size(); }
    void report(ostream& os);

    template <typename T>
    void add(T value) { info.push_back(value); }
    Action get(int i) { return info[i];}
};

class ActionSequence {
public:
    vector<Action> info;
    ActionSequence();
};


/*********************************************************/
/* ------------------- SYSTEM CLASS -------------------- */
/*********************************************************/

class System {
public:
    /* Attributes */
    Sim& sim; // Reference to the simulator
    SystemHistory history;
    SystemState* state_zero;
    SystemState* current;

    /* Function */
    System(Sim& _sim);
    void simulate();
    virtual void reset() = 0;
    virtual SystemState* create_initial_state() = 0;
    virtual void restore_state(SystemState& state) = 0;
    virtual SystemState* extract_state(SimTime t);
    virtual SystemState* interpolate_state(SimTime t, SystemState* nearest) = 0;
    virtual string hash_state(SystemState &state) = 0;
    virtual SystemState* unhash_state(string code) = 0;
    virtual string hash_action(Action& action) = 0;
    virtual Action get_action(int a) = 0;
    virtual void get_actions(SystemState& state, Actions& actions) = 0;
    virtual void report_actions(ostream& os) = 0;
    virtual void report_action(ostream& os, Action &action) = 0;
    virtual double take_action(SystemState& state, Action& action) = 0;
    virtual SimTime* get_next_decision_point(SystemState& state, bool exclude_current = false) = 0;
    virtual int get_index(Action& action) = 0;
    virtual void transition(SystemState& current_state, SimTime t) = 0;
    virtual int number_of_actions() = 0; 
    virtual bool is_terminal_state(SystemState& state) = 0;
};


