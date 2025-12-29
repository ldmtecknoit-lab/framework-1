{

    managers : (
        {"path": "framework/manager/messenger.py"; "service": "messenger"; "config": {"cache_enabled": True; "log_level": "INFO";}; "dependency_keys": ("message"); "messenger": "messenger"; },
        {"path": "framework/manager/executor.py"; "service": "executor"; "config": {"cache_enabled": True; "log_level": "INFO";}; "dependency_keys": ("actuator"); "messenger": "executor"; },
        {"path": "framework/manager/presenter.py"; "service": "presenter"; "config": {"cache_enabled": True; "log_level": "INFO";}; "dependency_keys": ("messenger"); "messenger": "presenter"; },
        {"path": "framework/manager/defender.py"; "service": "defender"; "config": {"cache_enabled": True; "log_level": "INFO";}; "dependency_keys": ("authentication"); "messenger": "defender"; },
        {"path": "framework/manager/storekeeper.py"; "service": "storekeeper"; "config": {"cache_enabled": True; "log_level": "INFO";}; "dependency_keys": ("persistence"); "messenger": "storekeeper"; },
        {"path": "framework/manager/tester.py"; "service": "tester"; "config": {"cache_enabled": True; "log_level": "INFO";}; "dependency_keys": ("messenger","persistence"); "messenger": "tester"; }
    );

    services : (
        {"path": "infrastructure/message/console.py"; "service": "message"; "adapter": "adapter"; "payload": config;}
    );
    
    # Configurazione globale
    configuration : "pyproject.toml" | resource | format | convert(dict, "toml");
    ports : ("presentation", "persistence", "message", "authentication", "actuator","authorization");
    
    # 2. Funzione per registrare un singolo driver
    register_driver : (driver_name,driver_data), {
        # Using functional syntax: get(dict, key)
        adapter_data : get(driver_data, "adapter");
        adapter_name : adapter_data;
        
        # Costruisce oggetto per register
        payload_data : adapter_data | merge({"profile": driver_name; "project": "default";}) | merge(
        {
            "path": "infrastructure/" + module_name + "/" + adapter_name + ".py";
            "service": module_name;
            "adapter": "adapter";
            "payload": payload_data;
        });
        
        res : register(payload_data);
    }, (res);

    # 3. Funzione per processare un modulo
    process_module : (module_name), {
        # Accesso alla config globale
        mod_config : configuration | get(module_name);
        
        # Ottiene keys
        driver_names : keys(mod_config);
        
        # Itera: foreach(iterable, func)
        res : foreach(driver_names, register_driver);
    }, (res);

    # 4. Esecuzione: foreach(ports, process_module)
    load_providers : ports|foreach(process_module);


}