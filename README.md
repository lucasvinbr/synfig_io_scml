# synfig_io_scml
Synfig plugin for exporting spriter .scml files.

Each root layer in the synfig scene should represent one spriter animation. 

Supports: 
- sprite layers
- sprite switch layers
- Nested layer transforming
- definition of a specific end time for an anim, via keyframes in synfig (create a "idle_end" keyframe in synfig and the "idle" animation will end in that keyframe's time)
