import math
import random

def layout_graph(nodes, edges, width, height, iterations=50):
    positions = {}
    for node in nodes:
        positions[node] = [random.randint(0, width), random.randint(0, height)]

    # k is ideal distance between nodes
    k = math.sqrt(width * height / len(nodes)) if nodes else 1
    t = width / 10

    def f_a(d):
        return d * d / k

    def f_r(d):
        if d == 0:
            return 1000
        return k * k / d

    for i in range(iterations):
        displacements = {n: [0, 0] for n in nodes}

        # Repulsive
        for v in nodes:
            for u in nodes:
                if u != v:
                    dx = positions[v][0] - positions[u][0]
                    dy = positions[v][1] - positions[u][1]
                    dist = math.hypot(dx, dy)
                    if dist > 0:
                        rep = f_r(dist)
                        displacements[v][0] += (dx / dist) * rep
                        displacements[v][1] += (dy / dist) * rep

        # Attractive
        for u, v in edges:
            dx = positions[v][0] - positions[u][0]
            dy = positions[v][1] - positions[u][1]
            dist = math.hypot(dx, dy)
            if dist > 0:
                attr = f_a(dist)
                displacements[v][0] -= (dx / dist) * attr
                displacements[v][1] -= (dy / dist) * attr
                displacements[u][0] += (dx / dist) * attr
                displacements[u][1] += (dy / dist) * attr

        # Update
        for v in nodes:
            dx, dy = displacements[v]
            dist = math.hypot(dx, dy)
            if dist > 0:
                limited_dist = min(dist, t)
                new_x = positions[v][0] + (dx / dist) * limited_dist
                new_y = positions[v][1] + (dy / dist) * limited_dist
                # Keep in bounds
                positions[v] = [min(width - 50, max(0, new_x)), min(height - 20, max(0, new_y))]
        
        t = max(t * 0.9, 1)

    return positions

print(layout_graph(['A', 'B', 'C'], [('A', 'B'), ('B', 'C')], 200, 100))
