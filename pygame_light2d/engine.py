import moderngl
import pygame
import numpy as np
from enum import Enum
from OpenGL.GL import glBlitNamedFramebuffer, GL_COLOR_BUFFER_BIT, GL_NEAREST

from light import PointLight
from hull import Hull


class Layer(Enum):
    BACKGROUND = 1,
    FOREGROUND = 2,


BACKGROUND = Layer.BACKGROUND
FOREGROUND = Layer.FOREGROUND


class LightingEngine:

    def __init__(self, width: int, height: int) -> None:
        # Screen resolution
        self.width = width
        self.height = height

        # Ambient light
        self.ambient = (0., 0., 0., .5)

        # Light and hull lists
        self.lights: list[PointLight] = []
        self.hulls: list[Hull] = []

        # Configure pygame
        if not pygame.get_init():
            pygame.init()

        pygame.display.set_mode(
            (width, height), pygame.HWSURFACE | pygame.OPENGL | pygame.DOUBLEBUF)

        # Create an OpenGL context
        self.ctx = moderngl.create_context()
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = self.ctx.SRC_ALPHA, self.ctx.ONE_MINUS_SRC_ALPHA

        # Load shaders
        vertex_filepath = 'shaders/vertex.glsl'
        fragment_filepath_light = 'shaders/fragment_light.glsl'
        fragment_filepath_blur = 'shaders/fragment_blur.glsl'
        fragment_filepath_mask = 'shaders/fragment_mask.glsl'
        fragment_filepath_draw = 'shaders/fragment_draw.glsl'

        with open(vertex_filepath, 'r') as f:
            vertex_src = f.read()

        with open(fragment_filepath_light, 'r') as f:
            fragment_src_light = f.read()

        with open(fragment_filepath_blur, 'r') as f:
            fragment_src_blur = f.read()

        with open(fragment_filepath_mask, 'r') as f:
            fragment_src_mask = f.read()

        with open(fragment_filepath_draw, 'r') as f:
            fragment_src_draw = f.read()

        self.prog_light = self.ctx.program(vertex_shader=vertex_src,
                                           fragment_shader=fragment_src_light)

        self.prog_blur = self.ctx.program(vertex_shader=vertex_src,
                                          fragment_shader=fragment_src_blur)

        self.prog_mask = self.ctx.program(vertex_shader=vertex_src,
                                          fragment_shader=fragment_src_mask)

        self.prog_draw = self.ctx.program(vertex_shader=vertex_src,
                                          fragment_shader=fragment_src_draw)

        # Screen mesh
        vertices = np.array([(-1.0, 1.0), (1.0, 1.0), (-1.0, -1.0),
                            (-1.0, -1.0), (1.0, 1.0), (1.0, -1.0)], dtype=np.float32)
        tex_coords = np.array([(0.0, 1.0), (1.0, 1.0), (0.0, 0.0),
                               (0.0, 0.0), (1.0, 1.0), (1.0, 0.0)], dtype=np.float32)
        vertex_data = np.hstack([vertices, tex_coords])

        # VAO and VBO for screen mesh
        vbo = self.ctx.buffer(vertex_data)
        self.vao_light = self.ctx.vertex_array(self.prog_light, [
            (vbo, '2f 2f', 'vertexPos', 'vertexTexCoord'),
        ])
        self.vao_blur = self.ctx.vertex_array(self.prog_blur, [
            (vbo, '2f 2f', 'vertexPos', 'vertexTexCoord'),
        ])
        self.vao_mask = self.ctx.vertex_array(self.prog_mask, [
            (vbo, '2f 2f', 'vertexPos', 'vertexTexCoord'),
        ])

        # Frame buffers
        self._tex_bg = self.ctx.texture((width, height), components=4)
        self._tex_bg.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self._fbo_bg = self.ctx.framebuffer([self._tex_bg])

        self.tex_fg = self.ctx.texture((width, height), components=4)
        self.tex_fg.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self._fbo_fg = self.ctx.framebuffer([self.tex_fg])

        self.tex_lt = self.ctx.texture((width, height), components=4)
        self.tex_lt.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._fbo_lt = self.ctx.framebuffer([self.tex_lt])
        # downscale for free AA

        # Ambient occlussion map
        self.tex_ao = self.ctx.texture((width, height), components=4)
        self.tex_ao.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self._fbo_ao = self.ctx.framebuffer([self.tex_ao])

        # SSBO for hull vertices
        self.ssbo_v = self.ctx.buffer(reserve=4*2048)
        self.ssbo_v.bind_to_uniform_block(1)
        self.ssbo_i = self.ctx.buffer(reserve=4*256)
        self.ssbo_i.bind_to_uniform_block(2)

    # TEMP
    def _point_to_uv(self, p):
        return [p[0]/self.width, 1 - (p[1]/self.height)]

    def load_texture(self, path: str) -> moderngl.Texture:
        img = pygame.image.load(path).convert_alpha()
        img_flip = pygame.transform.flip(img, False, True)
        img_data = pygame.image.tostring(img_flip, 'RGBA')

        tex = self.ctx.texture(size=img.get_size(),
                               components=4, data=img_data)
        tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        return tex

    def blit_texture(self, tex: moderngl.Texture, layer: Layer, dest: pygame.Rect, source: pygame.Rect):
        # Create a framebuffer with the texture
        fb = self.ctx.framebuffer([tex])

        # Select destination framebuffer correcponding to layer
        if layer == Layer.BACKGROUND:
            fbo = self._fbo_bg
        elif layer == Layer.FOREGROUND:
            fbo = self._fbo_fg

        # Blit texture onto destination
        glBlitNamedFramebuffer(fb.glo, fbo.glo, source.x, source.y, source.w, source.h,
                               dest.x, dest.y, dest.w, dest.h, GL_COLOR_BUFFER_BIT, GL_NEAREST)

    def render_texture(self, tex: moderngl.Texture, layer: Layer, dest: pygame.Rect, source: pygame.Rect):
        # Mesh for destination rect on screen
        width, height = self.ctx.screen.size
        x = 2. * dest.x / width - 1.
        y = 1. - 2. * dest.y / height
        w = 2. * dest.w / width
        h = 2. * dest.h / height
        vertices = np.array([(x, y), (x + w, y), (x, y - h),
                            (x, y - h), (x + w, y), (x + w, y - h)], dtype=np.float32)

        # Mesh for source within the texture
        x = source.x / tex.size[0]
        y = source.y / tex.size[1]
        w = source.w / tex.size[0]
        h = source.h / tex.size[1]

        p1 = (x, y + h)
        p2 = (x + w, y + h)
        p3 = (x, y)
        p4 = (x + w, y)
        tex_coords = np.array([p1, p2, p3,
                               p3, p2, p4], dtype=np.float32)

        # Create VBO and VAO
        buffer_data = np.hstack([vertices, tex_coords])

        vbo = self.ctx.buffer(buffer_data)
        vao = self.ctx.vertex_array(self.prog_draw, [
            (vbo, '2f 2f', 'vertexPos', 'vertexTexCoord'),
        ])

        # Render texture onto layer with the draw shader
        if layer == Layer.BACKGROUND:
            fbo = self._fbo_bg
        elif layer == Layer.FOREGROUND:
            fbo = self._fbo_fg

        tex.use()
        fbo.use()
        vao.render()

    # Clear background
    def clear(self, R=0, G=0, B=0, A=1):
        self._fbo_bg.clear(R, G, B, A)

    def render(self):
        # Clear intermediate buffers
        self._fbo_fg.clear(0, 0, 0, 0)
        self._fbo_ao.clear(0, 0, 0, 0)
        self._fbo_lt.clear(0, 0, 0, 0)

        # SSBO with hull vertices and their indices
        vertices = []
        indices = []
        for hull in self.hulls:
            vertices += hull.vertices
            indices.append(len(vertices))

        vertices = [self._point_to_uv(v) for v in vertices]
        nvertices = len(vertices)
        data_v = np.array(vertices, dtype=np.float32).flatten().tobytes()
        self.ssbo_v.write(data_v)

        nindices = len(indices)
        data_i = np.array(indices, dtype=np.int32).flatten().tobytes()
        self.ssbo_i.write(data_i)

        self.ctx.disable(moderngl.BLEND)
        # Send uniforms to light shader
        # TODO: point_to_coord should be GONE in the future!!
        for light in self.lights:
            # Skip light if disabled
            if not light.enabled:
                continue

            # Use lightmap
            self._fbo_lt.use()
            self.tex_lt.use()

            # Send light uniforms
            self.prog_light['lightPos'] = self._point_to_uv(light.position)
            self.prog_light['lightCol'] = light._color
            self.prog_light['lightPower'] = light.power
            self.prog_light['decay'] = light.decay

            # hull uniforms
            self.prog_light['hullVSSBO'].binding = 1
            # self.prog_light['numV'] = nvertices
            self.prog_light['hullIndSSBO'].binding = 2
            self.prog_light['numInd'] = nindices

            # Render onto aomap
            self.vao_light.render()
        self.ctx.enable(moderngl.BLEND)

        # Blur lightmap for soft shadows and render onto aomap
        self._fbo_ao.use()
        self.tex_lt.use()
        # self.prog_blur['lightPos'] = self._point_to_coord(light.position)
        self.vao_blur.render()

        # Render background masked with the lightmap
        self.ctx.screen.use()
        self._tex_bg.use()

        self.tex_ao.use(1)
        self.prog_mask['lightmap'].value = 1

        self.vao_mask.render()

    def surface_to_texture(self, sfc: pygame.Surface):
        tex = self.ctx.texture(sfc.get_size(), 4)
        tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        tex.swizzle = 'BGRA'
        tex.write(sfc.get_view('1'))
        return tex