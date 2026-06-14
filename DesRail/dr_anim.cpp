#include "dr_anim.h"
#include "comparison_tolerance.h"
#include <string>

using namespace std;
using namespace desv;
using namespace comparison_tolerance;

namespace dra {

    RTTAnimMgr::RTTAnimMgr(RailNetwork* _network,
        Sim& _sim, string scriptf,
        string _spritef, string _car_spritef,
        double _sprite_scale, double _sprite_w, double _sprite_h,
        double _car_w, double _car_h,
        double _anim_start_t,
        double _anim_end_t) :

        SimObject("dr_anim_mgr", _sim), network(_network), script(scriptf, _sim),
        anim_start_t(_anim_start_t), anim_end_t(_anim_end_t),
        active_trains(_network->signals_mgr->active_trains),
        train_sprite_scale(_sprite_scale), sprite_w(_sprite_w), sprite_h(_sprite_h),
        car_w(_car_w), car_h(_car_h),
        sprite_idx(0)
    {
        spritef.push_back(_spritef);
        car_spritef.push_back(_car_spritef);
        lock_colours.emplace_back(255, 0, 0);

        for (auto arc : network->arcs)
            script.set_path_true_length(arc->str_id, arc->segment->length);
    }

    SimCoroutine RTTAnimMgr::run()
    {
        co_await delay(anim_start_t);
        set<Train*> known_trains;

        int train_count = 0;
        while (true) {
            co_await wait_until([this, train_count]() { return active_trains.size() != train_count || sim.time_now >= anim_end_t; });

            if (sim.time_now >= anim_end_t)
                break;

            if (active_trains.size() > train_count) {
                for (auto train : active_trains) {
                    auto it = known_trains.find(train);
                    if (it == known_trains.end()) {
                        known_trains.insert(train);
                        train_anims.emplace_back(this, train, spritef[sprite_idx], car_spritef[sprite_idx], lock_colours[sprite_idx]);
                        sprite_idx = sprite_idx == spritef.size() - 1 ? 0 : sprite_idx + 1;
                    }
                }//trains
            }
            else {
                for (auto it = train_anims.begin(); it != train_anims.end();) {
                    TrainAnim& train_anim = *it;
                    auto it2 = active_trains.find(train_anim.train);
                    if (it2 == active_trains.end()) {
                        it = train_anims.erase(it);
                        auto it3 = known_trains.find(train_anim.train);
                        known_trains.erase(it3);
                    }
                    else
                        ++it;
                }//train_anim
            }

            train_count = active_trains.size();
        }//wend

        //script.write();
        co_return;
    }

    void RTTAnimMgr::add_spritef(string _spritef, string _car_spritef, int lock_r, int lock_g, int lock_b)
    {
        spritef.push_back(_spritef);
        car_spritef.push_back(_car_spritef);
        lock_colours.emplace_back(lock_r, lock_g, lock_b);
    }

    TrainAnim::TrainAnim(RTTAnimMgr* _mgr, Train* _train, string _spritef, string _car_spritef, rgb _lock_colour) :
        SimObject(_train->descr + string("_a"), _train->sim),
        mgr(_mgr),
        spritef(_spritef),
        car_spritef(_car_spritef),
        lock_colour(_lock_colour),
        train(_train),
        obj_id("")
    {
        activate();
    }

    SimCoroutine TrainAnim::run()
    {
        record_state();

        //initial placement of train sprite
        for (int i = train->cars.size() - 1; i > 0; --i) {     //reverse order so loco ends up being layered on top
            obj_id = "car" + to_string(train->cars[i]->id);
            mgr->script.add_object(obj_id, car_spritef, mgr->train_sprite_scale, 0.0, 0.0, false);
            mgr->script.set_object_guides(obj_id, mgr->car_w / 2.0, mgr->car_h, 0);
            mgr->script.set_object_true_length(obj_id, train->cars[i]->length, false);  //change to true for animation to scale
        }
        obj_id = "car" + to_string(train->cars[0]->id);
        mgr->script.add_object(obj_id, spritef, mgr->train_sprite_scale, 0.0, 0.0, false);
        mgr->script.set_object_guides(obj_id, mgr->sprite_w / 2.0, mgr->sprite_h, 0);
        mgr->script.set_object_true_length(obj_id, train->cars[0]->length, false);  //change to true for animation to scale

        mgr->script.place_object_on_path(obj_id, f_arc->str_id, true, (f_arc->segment->length - train->front_pos)/f_arc->segment->length);
        mgr->script.custom("update_arc_state", vector<string>({ f_arc->str_id, "1" }));

        string leader_id = obj_id;
        for (int i = 1; i < train->cars.size(); i++) {
            string follower_id = "car" + to_string(train->cars[i]->id);
            mgr->script.follow_leader(follower_id, leader_id);
            leader_id = follower_id;
        }

        SpeedProfileArc* sp_arc = nullptr;
        while (true) {
            co_await wait_until([this]() { return something_changed(); });

            if (b_arc != train->arcs.back() && b_arc->owner == nullptr)
                mgr->script.custom("update_arc_state", vector<string>({ b_arc->str_id, "0" }));

            if (f_arc != train->arcs.front() || train->current_acceleration != a_inst || train->section != section) {
                if (eq(train->current_acceleration, 0, train->eps) && eq(train->v_inst, 0, train->eps)) {
                    record_state();
                    continue;   //train is stationary, nothing to do
                }

                if (train->section != section) {
                    for (auto s_arc : train->section->arcs)
                        mgr->script.custom("update_arc_state", vector<string>({ s_arc->str_id, "1", lock_colour.rstr, lock_colour.gstr, lock_colour.bstr }));
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
                    continue;
                }

                mgr->script.place_object_on_path(obj_id, arc->str_id, true, (arc->segment->length - train->front_pos)/arc->segment->length);

                if (eq(train->current_acceleration, 0, train->eps)) {
                    mgr->script.move_object_on_path(obj_id, train->arcs.front()->str_id, t, 1, 1, move_end);
                }
                else {
                    double nrm_accel = train->current_acceleration / arc->segment->length;
                    double nrm_v0 = train->v_inst / arc->segment->length;
                    mgr->script.accel_object_on_path(obj_id, train->arcs.front()->str_id, t, 1, 1, move_end,
                        nrm_accel, nrm_v0);
                }

            }
            else {

                }

            if (train->finished) {
                //TODO: delete object
                break;
            }

            if (sim.time_now > mgr->anim_end_t)
                break;
            record_state();
        }//wend
        co_return;
    }

    bool TrainAnim::something_changed()
    {
        return section != train->section ||
            train->arcs.front() != f_arc ||
            train->arcs.back() != b_arc ||
            train->current_acceleration != a_inst ||
            train->section != section ||
            train->finished;
    }

    void TrainAnim::record_state()
    {
        f_arc = train->arcs.front();
        b_arc = train->arcs.back();
        section = train->section;
        v_inst = train->v_inst;
        a_inst = train->current_acceleration;
        t_obs = sim.time_now;
    }


}//dra