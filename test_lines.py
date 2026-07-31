def draw_lines(width, height, edges_coords):
    grid = [[' ' for _ in range(width)] for _ in range(height)]
    
    def plot_line(x0, y0, x1, y1):
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        
        while True:
            if 0 <= int(y0) < height and 0 <= int(x0) < width:
                grid[int(y0)][int(x0)] = '*'
            if int(x0) == int(x1) and int(y0) == int(y1):
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy
                
    for (x0, y0), (x1, y1) in edges_coords:
        plot_line(x0, y0, x1, y1)
        
    return '\n'.join(''.join(row) for row in grid)

print(draw_lines(20, 10, [((2,2), (18,8))]))
