import math
import random

def draw_ortho_lines(width, height, edges_coords):
    grid = [[' ' for _ in range(width)] for _ in range(height)]
    
    def plot_hline(x0, x1, y):
        if not (0 <= y < height): return
        start_x, end_x = min(x0, x1), max(x0, x1)
        for x in range(start_x, end_x + 1):
            if 0 <= x < width:
                if grid[y][x] == ' ': grid[y][x] = '-'
    
    def plot_vline(x, y0, y1):
        if not (0 <= x < width): return
        start_y, end_y = min(y0, y1), max(y0, y1)
        for y in range(start_y, end_y + 1):
            if 0 <= y < height:
                if grid[y][x] in (' ', '-'): grid[y][x] = '|'
    
    def plot_corner(x, y, char):
        if 0 <= y < height and 0 <= x < width:
            grid[y][x] = char
            
    for (x0, y0), (x1, y1) in edges_coords:
        mid_x = (x0 + x1) // 2
        
        plot_hline(x0, mid_x, y0)
        plot_vline(mid_x, y0, y1)
        plot_hline(mid_x, x1, y1)
        
        plot_corner(mid_x, y0, '+')
        plot_corner(mid_x, y1, '+')

    return '\n'.join(''.join(row) for row in grid)

print(draw_ortho_lines(30, 20, [((2,2), (28,8)), ((5,18), (15,5))]))
