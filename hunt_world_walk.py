"""Use normal, audited area exits to travel between hunting maps."""
from collections import deque
import json
import math
from pathlib import Path

from hunt_navigation import load_path_map, plan_hunt_path


def world_route(base, scene, position, destination, level):
    path_map = load_path_map(base,scene)
    region = path_map.locate(path_map.routing_position(position))[1]
    edges = json.loads((Path(base)/"world_walk_routes.json").read_text(encoding="utf-8"))["edges"]
    start = (scene,region)
    parents, queue, goal = {start:None}, deque([start]), None
    while queue:
        current = queue.popleft()
        if current[0] == destination:
            goal = current
            break
        for edge in edges:
            if (edge["source_scene"],edge["source_region"]) != current:
                continue
            if level < edge["min_level"] or (edge["max_level"] and level >= edge["max_level"]):
                continue
            next_state = (edge["target_scene"],edge["target_region"])
            if next_state not in parents:
                parents[next_state] = current,edge
                queue.append(next_state)
    if goal is None:
        raise ValueError("当前地图位置没有通往目的地图的普通出口路线")
    route = []
    while goal != start:
        previous,edge = parents[goal]
        route.append(edge)
        goal = previous
    return list(reversed(route))


def followed_team_ready(status, expected_guids, expected_count):
    return (status is not None and status["follow"] == 1 and status["missing"] == 0
            and expected_guids.issubset(status["guids"])
            and len(status["guids"]) >= expected_count)


def follow_restart_safe(base, scene, position):
    data = json.loads((Path(base)/"world_walk_routes.json").read_text(encoding="utf-8"))
    bounds = data.get("follow_restart_exclusion_areas",{}).get(str(scene))
    if bounds is None:
        return False
    x,y = position
    return not any(left-2. <= x <= right+2. and top-2. <= y <= bottom+2.
                   for left,top,right,bottom in bounds)


def recovery_path_safe(base, scene, start, nodes):
    data=json.loads((Path(base)/"world_walk_routes.json").read_text(encoding="utf-8"))
    bounds=data.get("follow_restart_exclusion_areas",{}).get(str(scene))
    if bounds is None:return False
    for a,b in zip([start]+nodes,nodes):
        for left,top,right,bottom in bounds:
            low,high=0.,1.
            # Include the uncertainty of a truncated coordinate in this margin.
            for k,minimum,maximum in ((0,left-3.5,right+3.5),(1,top-3.5,bottom+3.5)):
                delta=b[k]-a[k]
                if abs(delta)<1e-12:
                    if not minimum<=a[k]<=maximum:low,high=1.,0.;break
                else:
                    first,second=sorted(((minimum-a[k])/delta,(maximum-a[k])/delta))
                    low,high=max(low,first),min(high,second)
            if low<=high:return False
    return True


def hunt_recovery_step(base, scene, position):
    mesh=load_path_map(base,scene)
    anchor=mesh.routing_position(position)
    region=mesh.locate(anchor)[1]
    candidates=sorted((abs(math.dist(anchor,mesh.centers[i])-5.),mesh.centers[i])
                      for i in mesh.regions[region] if 3.<=math.dist(anchor,mesh.centers[i])<=8.)
    for _,point in candidates[:24]:
        try:nodes=plan_hunt_path(base,scene,position,point,0.)
        except ValueError:continue
        if (nodes and nodes[-1]==point
                and sum(math.dist(a,b) for a,b in zip([anchor]+nodes,nodes))<=12.
                and recovery_path_safe(base,scene,position,nodes)):
            return point
    raise ValueError("当前位置没有已验证的短距离防卡路线")
