"""Plan routes inside the verified test server's triangular path maps."""
import hashlib
import heapq
import json
import math
from pathlib import Path
import struct


def cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def polygon_area(points):
    return abs(sum(points[i][0]*points[(i+1)%len(points)][1]
                   - points[(i+1)%len(points)][0]*points[i][1]
                   for i in range(len(points))))/2 if len(points)>2 else 0.


def clip_cell(points, bounds):
    for axis, limit, direction in ((0,bounds[0],1),(0,bounds[1],-1),
                                    (1,bounds[2],1),(1,bounds[3],-1)):
        result=[]
        for a,b in zip(points,points[1:]+points[:1]):
            inside_a=direction*(a[axis]-limit)>=0
            inside_b=direction*(b[axis]-limit)>=0
            if inside_a:result.append(a)
            if inside_a != inside_b:
                t=(limit-a[axis])/(b[axis]-a[axis])
                result.append(tuple(a[k]+t*(b[k]-a[k]) for k in (0,1)))
        points=result
        if not points:break
    return points


def convex_hull(points):
    points=sorted(set(points))
    halves=[]
    for ordered in (points,list(reversed(points))):
        half=[]
        for point in ordered:
            while len(half)>=2 and cross(half[-2],half[-1],point)<=0:half.pop()
            half.append(point)
        halves.append(half[:-1])
    return halves[0]+halves[1]


def clip_inside_edge(points, a, b, orientation):
    result=[]
    for p,q in zip(points,points[1:]+points[:1]):
        left=orientation*cross(a,b,p);right=orientation*cross(a,b,q)
        if left>=0:result.append(p)
        if (left>=0) != (right>=0):
            t=left/(left-right)
            result.append(tuple(p[k]+t*(q[k]-p[k]) for k in (0,1)))
    return result


class PathMap:
    def __init__(self, data):
        if len(data) < 10 or struct.unpack_from("<H", data)[0] != 0x2000:
            raise ValueError("寻路地图版本未确认")
        count = struct.unpack_from("<I", data, 2)[0]
        if not 3 <= count <= 100000 or 6 + count*8 + 4 > len(data):
            raise ValueError("寻路地图顶点数量无效")
        self.vertices = list(struct.iter_unpack("<ff", data[6:6+count*8]))
        if any(not all(math.isfinite(v) and abs(v) <= 100000 for v in p) for p in self.vertices):
            raise ValueError("寻路地图坐标无效")
        offset = 6 + count*8
        triangles = struct.unpack_from("<I", data, offset)[0]
        if not 1 <= triangles <= 200000 or offset+4+triangles*29+4 > len(data):
            raise ValueError("寻路地图三角形数量无效")
        self.records = list(struct.iter_unpack("<6IBI", data[offset+4:offset+4+triangles*29]))
        self.points, self.centers, self.regions = [], [], {}
        for i, r in enumerate(self.records):
            if max(r[:3]) >= count or len(set(r[:3])) != 3:
                raise ValueError("寻路地图顶点索引无效")
            if (r[6] == 0 and r[7] > 0):
                self.regions.setdefault(r[7], []).append(i)
            elif r[6] not in (1,3) or r[7] != 0:
                raise ValueError("寻路地图区域标记未确认")
            p = tuple(self.vertices[j] for j in r[:3])
            if abs(cross(*p)) < 1e-8:
                raise ValueError("寻路地图含退化三角形")
            self.points.append(p)
            self.centers.append(tuple(sum(v[k] for v in p)/3 for k in (0,1)))
        for i, r in enumerate(self.records):
            for neighbor in r[3:6]:
                if neighbor == 1000000:
                    continue
                if (neighbor >= triangles or i not in self.records[neighbor][3:6]
                        or len(set(r[:3]) & set(self.records[neighbor][:3])) != 2):
                    raise ValueError("寻路地图相邻三角形校验失败")

    def locate(self, position):
        for region, indices in self.regions.items():
            for i in indices:
                p = self.points[i]
                values = [cross(p[j],p[(j+1)%3],position) for j in range(3)]
                if min(values) >= -1e-6 or max(values) <= 1e-6:
                    return i, region
        raise ValueError("起点或终点不在可行走区域")

    def routing_position(self, position):
        try:
            self.locate(position)
            return position
        except ValueError:
            pass
        # GetWorldPos truncates each float. Resolve only that exact coordinate
        # cell, and only if its walkable intersection has a visibility kernel.
        # This is a routing anchor, not a fabricated precise player position.
        if any(v != math.trunc(v) or v == 0 for v in position):
            raise ValueError("起点或终点不在可行走区域")
        x,y=position
        bounds=(x if x>0 else x-1,x+1 if x>0 else x,
                y if y>0 else y-1,y+1 if y>0 else y)
        pieces=[];regions=set();indices_in_cell=[]
        for region,indices in self.regions.items():
            for i in indices:
                polygon=clip_cell(list(self.points[i]),bounds)
                if polygon_area(polygon)>1e-8:
                    regions.add(region);pieces.append(polygon);indices_in_cell.append(i)
        if len(regions)!=1:
            raise ValueError("整数坐标格内没有唯一可行走区域")
        hull=convex_hull([p for polygon in pieces for p in polygon])
        if abs(polygon_area(hull)-sum(polygon_area(p) for p in pieces))>1e-6:
            # Intersect the interior half-planes of actual walkable boundary
            # edges. A point in this kernel is reachable by a straight segment
            # from every walkable point in the cell, including concave corners.
            hull=[(bounds[0],bounds[2]),(bounds[1],bounds[2]),
                  (bounds[1],bounds[3]),(bounds[0],bounds[3])]
            for i in indices_in_cell:
                triangle=self.points[i];record=self.records[i]
                orientation=1 if cross(*triangle)>0 else -1
                for j in range(3):
                    edge={record[j],record[(j+1)%3]}
                    if any(n!=1000000 and self.records[n][7]==record[7]
                           and edge.issubset(self.records[n][:3]) for n in record[3:6]):
                        continue
                    a,b=triangle[j],triangle[(j+1)%3]
                    clipped=clip_cell([a,b],bounds)
                    if len(clipped)<2 or max(math.dist(p,q) for p in clipped for q in clipped)<1e-8:
                        continue
                    hull=clip_inside_edge(hull,a,b,orientation)
                    if polygon_area(hull)<1e-8:
                        raise ValueError("整数坐标格内障碍阻止安全靠近，暂停移动")
        anchor=tuple(sum(p[k] for p in hull)/len(hull) for k in (0,1))
        self.locate(anchor)
        return anchor

    def segment_clear(self, start, end, region):
        # Clip the segment against each triangle, then require full coverage.
        # A few sampled points would miss narrow walls and are insufficient.
        intervals = []
        for i in self.regions[region]:
            p = self.points[i]
            if (max(start[0],end[0]) < min(x[0] for x in p)
                    or min(start[0],end[0]) > max(x[0] for x in p)
                    or max(start[1],end[1]) < min(x[1] for x in p)
                    or min(start[1],end[1]) > max(x[1] for x in p)):
                continue
            orientation = 1 if cross(*p) > 0 else -1
            low, high = 0., 1.
            for j in range(3):
                a = orientation*cross(p[j],p[(j+1)%3],start)
                slope = orientation*cross(p[j],p[(j+1)%3],end) - a
                if abs(slope) < 1e-12:
                    if a < -1e-7:
                        low, high = 1., 0.
                        break
                elif slope > 0:
                    low = max(low, (-1e-7-a)/slope)
                else:
                    high = min(high, (-1e-7-a)/slope)
            if low <= high:
                intervals.append((max(0.,low),min(1.,high)))
        reach = 0.
        for low, high in sorted(intervals):
            if low > reach + 1e-7:
                return False
            reach = max(reach,high)
            if reach >= 1.-1e-7:
                return True
        return False

    def route(self, start, end):
        source, region = self.locate(start)
        target, target_region = self.locate(end)
        if target_region != region:
            raise ValueError("起点和终点属于不同的可行走区域")
        if self.segment_clear(start,end,region):
            return [end]
        costs, parents = {source:0.}, {}
        queue = [(math.dist(self.centers[source],self.centers[target]),0.,source)]
        while queue:
            _, cost, current = heapq.heappop(queue)
            if cost > costs[current]:
                continue
            if current == target:
                break
            for neighbor in self.records[current][3:6]:
                if neighbor == 1000000 or self.records[neighbor][7] != region:
                    continue
                new = cost + math.dist(self.centers[current],self.centers[neighbor])
                if new < costs.get(neighbor,math.inf):
                    costs[neighbor], parents[neighbor] = new,current
                    heapq.heappush(queue,(new+math.dist(self.centers[neighbor],self.centers[target]),new,neighbor))
        if target not in costs:
            raise ValueError("目的地没有可通行路线")
        corridor = [target]
        while corridor[-1] != source:
            corridor.append(parents[corridor[-1]])
        corridor.reverse()
        candidates = [start,self.centers[source]]
        for a,b in zip(corridor,corridor[1:]):
            edge = sorted(set(self.records[a][:3]) & set(self.records[b][:3]))
            candidates.append(tuple((self.vertices[edge[0]][k]+self.vertices[edge[1]][k])/2 for k in (0,1)))
            candidates.append(self.centers[b])
        candidates.append(end)
        result, index = [], 0
        while index < len(candidates)-1:
            next_index = len(candidates)-1
            while next_index > index and not self.segment_clear(candidates[index],candidates[next_index],region):
                next_index -= 1
            if next_index == index:
                raise ValueError("路线连续性校验失败")
            result.append(candidates[next_index])
            index = next_index
        return result


_cache = {}


def load_path_map(base_dir, scene):
    base = Path(base_dir)
    manifest = json.loads((base/"hunt_navigation/manifest.json").read_text(encoding="utf-8"))
    entry = manifest["maps"].get(str(scene))
    if entry is None or Path(entry["file"]).name != entry["file"]:
        raise ValueError("当前地图没有已验证的寻路数据")
    key = (str(base),scene,entry["sha256"])
    if key not in _cache:
        data = (base/"hunt_navigation"/entry["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("寻路地图文件校验失败")
        _cache[key] = PathMap(data)
    return _cache[key]


def plan_hunt_path(base_dir, scene, start, end, stop_distance):
    path_map=load_path_map(base_dir,scene)
    reported_start=start
    start=path_map.routing_position(start)
    end=path_map.routing_position(end)
    nodes = path_map.route(start,end)
    # Stop along the actual route, so the shortened destination stays inside
    # the walkable mesh even when the straight line to the monster is blocked.
    remaining = stop_distance
    while nodes and remaining > 0:
        previous = nodes[-2] if len(nodes)>1 else start
        length = math.dist(previous,nodes[-1])
        if length <= remaining:
            remaining -= length
            nodes.pop()
        else:
            ratio = (length-remaining)/length
            nodes[-1] = tuple(previous[k]+(nodes[-1][k]-previous[k])*ratio for k in (0,1))
            break
    if nodes and start != reported_start:
        nodes.insert(0,start)
    return nodes[:4]
