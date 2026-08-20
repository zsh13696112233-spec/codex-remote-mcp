package com.example.codex.config;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;
import org.springframework.http.*;
import org.springframework.web.bind.annotation.*;
import java.util.List;

@RestController @RequestMapping("/api")
class ApiController {
    private final ConfigService service;
    ApiController(ConfigService service){this.service=service;}

    @GetMapping("/roles") List<ObjectNode> roles(@RequestParam(defaultValue="") String q){return service.listRoles(q);}
    @PostMapping("/roles") @ResponseStatus(HttpStatus.CREATED) ObjectNode createRole(@RequestBody JsonNode b){return service.createRole(b);}
    @PutMapping("/roles/{id}") ObjectNode updateRole(@PathVariable String id,@RequestBody JsonNode b){return service.updateRole(id,b);}
    @DeleteMapping("/roles/{id}") @ResponseStatus(HttpStatus.NO_CONTENT) void deleteRole(@PathVariable String id){service.deleteRole(id);}

    @GetMapping("/sops") List<ObjectNode> sops(@RequestParam(defaultValue="") String q){return service.listSops(q);}
    @GetMapping("/sops/{id}") ObjectNode sop(@PathVariable String id){return service.getSop(id);}
    @PostMapping("/sops") @ResponseStatus(HttpStatus.CREATED) ObjectNode createSop(@RequestBody JsonNode b){return service.createSop(b);}
    @PutMapping("/sops/{id}") ObjectNode updateSop(@PathVariable String id,@RequestBody JsonNode b){return service.updateSop(id,b);}
    @DeleteMapping("/sops/{id}") @ResponseStatus(HttpStatus.NO_CONTENT) void deleteSop(@PathVariable String id){service.deleteSop(id);}

    @GetMapping("/task-definitions") List<ObjectNode> tasks(@RequestParam(defaultValue="") String q){return service.listTasks(q);}
    @GetMapping("/task-definitions/{id}") ObjectNode task(@PathVariable String id){return service.getTask(id);}
    @PostMapping("/task-definitions") @ResponseStatus(HttpStatus.CREATED) ObjectNode createTask(@RequestBody JsonNode b){return service.createTask(b);}
    @PutMapping("/task-definitions/{id}") ObjectNode updateTask(@PathVariable String id,@RequestBody JsonNode b){return service.updateTask(id,b);}
    @PostMapping("/task-definitions/{id}/copy") @ResponseStatus(HttpStatus.CREATED) ObjectNode copyTask(@PathVariable String id){return service.copyTask(id);}
    @DeleteMapping("/task-definitions/{id}") @ResponseStatus(HttpStatus.NO_CONTENT) void deleteTask(@PathVariable String id){service.deleteTask(id);}
    @PostMapping("/task-definitions/{id}/runs") ResponseEntity<ObjectNode> run(@PathVariable String id){return ResponseEntity.accepted().body(service.runLatest(id));}
    @GetMapping("/task-definitions/{id}/runs") List<ObjectNode> runs(@PathVariable String id){return service.listRuns(id);}
    @PostMapping("/task-runs/{workflowId}/cancel") ObjectNode cancel(@PathVariable String workflowId){return service.cancel(workflowId);}
    @PostMapping("/task-runs/{workflowId}/retry") ResponseEntity<ObjectNode> retry(@PathVariable String workflowId){return ResponseEntity.accepted().body(service.retry(workflowId));}

    @GetMapping("/agents") JsonNode agents(){return service.agents();}
    @GetMapping("/gateway/ready") JsonNode ready(){return service.ready();}
}
